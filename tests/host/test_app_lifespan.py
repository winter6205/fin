"""Acceptance tests for the application-owned resource lifetime (T4a).

``fin.host.http.create_app`` is the one composition that owns the
checkpointer, the four background workers and the run-control registry,
and this file pins the properties that ownership has to have to be worth
anything:

* importing the module starts nothing — no worker thread, no connection;
* two apps in one process share no state, and a run registered on one is
  not reachable through the other;
* the mounted workspace router and the attachment auto-resumer hold the
  SAME ``RunControl`` the app created, so an operator can stop a
  background run;
* a failure at ANY stage of the boot releases exactly what ran before it,
  once, in reverse, and re-raises;
* a repeated lifespan enter/exit leaks nothing.

Plus the compatibility surface the deployed spellings depend on: the
module-level ``app`` is still a FastAPI whose ``dependency_overrides``
still answer a real request, ``serve`` is still the ``fin-serve`` entry,
and a multi-process deployment is refused before anything is acquired.

Everything below runs through one seam: an injected ``StartupSteps``
of recording callables (or the deployed seams with only the opening ones
stubbed). That is enough to drive a real lifespan with no model,
database, worker thread or operator settings file.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import tomllib
from contextlib import ExitStack
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.application.lifetime import (
    SEAM_ACQUIRED,
    SEAM_PROCESS,
    SEAM_PROBE,
    WORKER_COUNT_ENV_VAR,
    ApplicationResources,
    RuntimeHandle,
    StartupSteps,
    UnsupportedProcessCountError,
    declared_process_count,
    require_single_process,
)
from fin.host.attachment.attachment_budgets import AttachmentBudgetConfigError
from fin.host.thread_store import InMemoryThreadStore

REPO_ROOT = Path(__file__).resolve().parents[2]

# Never created: the recording resume worker only hands it on.
UNUSED_ROOT = Path("unused-workspace-root")


class _StageFailure(RuntimeError):
    """The failure a case injects at one named stage of the boot."""


# ----- recording seams --------------------------------------------------

# The seams, read off the dataclass rather than written out again: a
# hand-copied list of field names can only confirm that the copy is
# still in step with itself. ``ownership`` is the classification of
# those seams, not a seam itself.
_STEP_NAMES = tuple(
    f.name for f in fields(StartupSteps) if f.name != "ownership"
)

# WHY the release of an acquiring seam is a property of the CONTRACT: a
# ``start_*`` seam is a thread with a ``stop_*`` twin, and the compiled
# runtime releases itself through the handle it returned. The owner
# decides WHICH acquisitions run, not what releases them — so a release
# it forgets shows up here as a missing entry instead of matching the
# owner's own list back to it.
_RELEASE_FOR_STEP = {
    name: f"stop_{name[len('start_'):]}"
    for name in _STEP_NAMES
    if name.startswith("start_")
}
_RELEASE_FOR_STEP["compile_runtime"] = "release_runtime"
_RELEASE_NAMES = frozenset(_RELEASE_FOR_STEP.values())


def _stages_through(fail_at: str) -> list[str]:
    """The boot stages that ran, up to and including ``fail_at``."""
    return list(_BOOT_STAGES[: _BOOT_STAGES.index(fail_at) + 1])


def _owed_releases(events: list[str], fail_at: str) -> list[str]:
    """The releases the acquisitions that actually ran owe, innermost first.

    Read off ``events``: a seam that ran and acquired owes its release,
    a seam that never ran owes nothing, and the owner has to produce
    them all. The seam that raised is the one exception — it failed, so
    it acquired nothing and pushed no release of its own.
    """
    acquired = [
        name
        for name in events
        if name in _RELEASE_FOR_STEP and name != fail_at
    ]
    return [_RELEASE_FOR_STEP[name] for name in reversed(acquired)]


def _recording_steps(
    events: list[str],
    *,
    fail_at: str | None = None,
    seen: dict[str, dict[str, Any]] | None = None,
    graph: Any = None,
    gates: dict[str, Any] | None = None,
    ownership: dict[str, str] | None = None,
) -> StartupSteps:
    """``StartupSteps`` whose every seam only records that it ran.

    ``fail_at`` names the stage that raises AFTER recording itself, so a
    case injects a failure at any point of the real boot order without
    building a model, a database or a thread. ``gates`` names the seams
    that run the DEPLOYED function behind the recorder, so the refusal
    under test is the real one while every other seam stays inert.
    ``seen`` keeps what each seam was called with. ``ownership`` carries
    a composition's own acquisition classification, so a case can drive
    the DEPLOYED statement rather than one written here.
    """
    real_gates = gates or {}

    def _record(name: str, result: Any = None) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            events.append(name)
            if seen is not None:
                seen[name] = {"args": args, "kwargs": kwargs}
            if fail_at == name:
                raise _StageFailure(name)
            gate = real_gates.get(name)
            return gate(*args, **kwargs) if gate is not None else result

        return _call

    return StartupSteps(
        require_single_process=_record("require_single_process"),
        apply_operator_settings=_record("apply_operator_settings"),
        require_checkpoint_url=_record("require_checkpoint_url"),
        attachment_budgets=_record("attachment_budgets", object()),
        receipt_export_config=_record("receipt_export_config"),
        preflight_ingest_storage=_record("preflight_ingest_storage"),
        publish_knowledge_mcp=_record("publish_knowledge_mcp"),
        compile_runtime=_record(
            "compile_runtime",
            RuntimeHandle(
                graph, release=lambda: events.append("release_runtime")
            ),
        ),
        start_ingest_worker=_record("start_ingest_worker"),
        thread_store=_record("thread_store", object()),
        attachment_service=_record("attachment_service", object()),
        start_attachment_prep_worker=_record("start_attachment_prep_worker"),
        start_attachment_resume_worker=_record("start_attachment_resume_worker"),
        start_invalidation_drain_worker=_record("start_invalidation_drain_worker"),
        start_receipt_export_worker=_record("start_receipt_export_worker"),
        stop_ingest_worker=_record("stop_ingest_worker"),
        stop_attachment_prep_worker=_record("stop_attachment_prep_worker"),
        stop_attachment_resume_worker=_record("stop_attachment_resume_worker"),
        stop_invalidation_drain_worker=_record("stop_invalidation_drain_worker"),
        stop_receipt_export_worker=_record("stop_receipt_export_worker"),
        workspace_root=_record("workspace_root", UNUSED_ROOT),
        ownership={} if ownership is None else dict(ownership),
    )


# The boot order is DISCOVERED, not assumed: one real boot through inert
# seams says which order this owner runs, so a case can inject a failure
# at every position the implementation actually has.
_BOOT_STAGES: tuple[str, ...] = ()


def _discover_boot_order() -> tuple[str, ...]:
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(events)
    )
    resources.startup()
    resources.shutdown()
    return tuple(
        name
        for name in events
        if name in _STEP_NAMES and name not in _RELEASE_NAMES
    )


_BOOT_STAGES = _discover_boot_order()


def _recording_app(
    events: list[str],
    *,
    seen: dict[str, dict[str, Any]] | None = None,
    gates: dict[str, Any] | None = None,
    graph: Any = None,
    process_count: int | None = None,
) -> FastAPI:
    """One app whose boot is entirely recording callables.

    A complete ``StartupSteps`` is the only injection seam: the graph,
    the store, the service and the root are seam RESULTS, so a case that
    wants one supplies it inside its own steps rather than through a
    second parameter on the factory.
    """
    from fin.host.http import create_app

    return create_app(
        steps=lambda: _recording_steps(
            events, seen=seen, gates=gates, graph=graph
        ),
        process_count=process_count,
    )


class _ObservableConnection:
    """Stands in for what the compile enters and this app must release."""

    def __init__(self) -> None:
        self.exits = 0

    def __enter__(self) -> "_ObservableConnection":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.exits += 1


def _stub_the_deployed_seams_that_open(
    monkeypatch: pytest.MonkeyPatch,
    *,
    compile_runtime: Any,
    acquired: list[str] | None = None,
    prep_worker: Any = None,
    workspace_root: Path = UNUSED_ROOT,
) -> None:
    """Leave the deployed boot running, inert only where it would acquire.

    ``create_app`` builds its own ``startup_steps``, so what a case
    below exercises is the composition this repository ships; only the
    seams that would need a database, a model, a child process or a
    thread are replaced. Every replaced seam records into ``acquired``,
    so a case can say the boot reached none of them. ``prep_worker`` is
    the seam that stays REAL, over the in-memory service the caller
    stubs in, so a repeated lifespan can be checked for thread growth.
    """
    import fin.corpus.ingest_storage as ingest_storage
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    ran = acquired if acquired is not None else []

    def _inert(name: str, result: Any = None) -> Any:
        def _call(*args: Any, **kwargs: Any) -> Any:
            ran.append(name)
            return result

        return _call

    # The boot refuses an undeclared durable checkpointer before it
    # opens anything, so this deployment has to state one. Nothing
    # connects to it: the compile seam is the only thing that would.
    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setattr(
        composition_module, "apply_operator_settings", _inert("apply_operator_settings")
    )
    monkeypatch.setattr(
        ingest_storage,
        "preflight_ingest_storage",
        _inert("preflight_ingest_storage"),
    )
    monkeypatch.setattr(
        composition_module, "publish_knowledge_mcp", _inert("publish_knowledge_mcp")
    )
    monkeypatch.setattr(
        composition_module, "attachment_budgets", _inert("attachment_budgets")
    )
    monkeypatch.setattr(
        composition_module,
        "_compile_http_runtime",
        lambda **_kwargs: compile_runtime(),
    )
    monkeypatch.setattr(
        composition_module,
        "get_workspace_root",
        lambda: _inert("workspace_root", workspace_root)(),
    )
    monkeypatch.setattr(
        composition_module,
        "start_application_ingest_worker",
        _inert("start_ingest_worker"),
    )
    monkeypatch.setattr(composition_module, "stop_ingest_worker", lambda: None)
    monkeypatch.setattr(
        composition_module,
        "start_application_attachment_resume_worker",
        lambda **kwargs: ran.append("start_attachment_resume_worker"),
    )
    monkeypatch.setattr(composition_module, "stop_attachment_resume_worker", lambda: None)
    monkeypatch.setattr(
        composition_module,
        "start_application_invalidation_drain_worker",
        lambda **kwargs: ran.append("start_invalidation_drain_worker"),
    )
    monkeypatch.setattr(
        composition_module, "stop_invalidation_drain_worker", lambda: None
    )
    if prep_worker is None:
        monkeypatch.setattr(
            composition_module,
            "start_application_attachment_prep_worker",
            lambda service: ran.append("start_attachment_prep_worker"),
        )
        monkeypatch.setattr(
            composition_module, "stop_attachment_prep_worker", lambda: None
        )
        monkeypatch.setattr(
            composition_module, "default_attachment_service", _inert("attachment_service")
        )
        monkeypatch.setattr(
            composition_module, "default_thread_store", _inert("thread_store", object())
        )
    else:
        monkeypatch.setattr(
            composition_module, "default_attachment_service", lambda: prep_worker
        )
        monkeypatch.setattr(
            composition_module, "default_thread_store", InMemoryThreadStore
        )


@pytest.fixture(autouse=True)
def _undeclared_process_count(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every case here is a single-process deployment unless it says so."""
    monkeypatch.delenv(WORKER_COUNT_ENV_VAR, raising=False)


# ----- importing the server module starts nothing -----------------------

# WHY a subprocess: most of this package's own fixtures import
# ``fin.host.http``, so only a fresh interpreter can say whether the
# import alone opened anything.
_IMPORT_PROBE = """
import json
import threading

import psycopg
import psycopg_pool

opened = []


def _refuse(*args, **kwargs):
    opened.append("psycopg.connect")
    raise AssertionError("import opened a database connection")


psycopg.connect = _refuse
psycopg_pool.ConnectionPool = _refuse

before = {thread.name for thread in threading.enumerate()}
import fin.host.http as http_module
after = {thread.name for thread in threading.enumerate()}

print(json.dumps({
    "opened": opened,
    "new_threads": sorted(after - before),
    "app": type(http_module.app).__name__,
    "serve": callable(http_module.serve),
}))
"""


def test_importing_the_server_module_opens_no_connection_and_no_thread() -> None:
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "FIN_CHECKPOINT_URL",
            "FIN_TEST_DSN",
            "FIN_SCRIPTED_MODEL",
            WORKER_COUNT_ENV_VAR,
        }
    }

    completed = subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout.strip().splitlines()[-1])
    assert report["opened"] == []
    assert report["new_threads"] == []
    assert report["app"] == "FastAPI"
    assert report["serve"] is True


# ----- one process, two isolated applications ---------------------------


def test_two_applications_do_not_share_a_run_registry() -> None:
    first = _recording_app([], graph=object())
    second = _recording_app([], graph=object())
    first_control = first.state.fin_resources.run_control
    second_control = second.state.fin_resources.run_control

    assert first_control is not second_control

    token = first_control.begin("t-1", "run-a")
    assert second_control.request_stop("t-1") is None
    assert token.is_set() is False
    assert first_control.request_stop("t-1") == "run-a"
    assert token.is_set() is True


def test_each_application_serves_its_owned_graph_after_its_peer_shuts_down(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A request uses its app's graph, and closing a peer leaves it live."""
    import fin.host.application.composition as composition_module
    from fin.host import http as http_module

    class _NamedGraph:
        def __init__(self, name: str) -> None:
            self.name = name

        def invoke(
            self, _state: Any, *, config: dict[str, Any]
        ) -> dict[str, Any]:
            return {"status": "done", "answer": self.name, "citations": []}

    legacy_graph = _NamedGraph("legacy singleton")
    monkeypatch.setattr(
        composition_module, "compile_for_http", lambda **_kwargs: legacy_graph
    )
    first_graph = _NamedGraph("first app")
    second_graph = _NamedGraph("second app")
    first = _recording_app([], graph=first_graph)
    second = _recording_app([], graph=second_graph)

    with TestClient(second) as second_client:
        with TestClient(first) as first_client:
            first_response = first_client.post("/ask", json={"question": "q"})
            second_response = second_client.post("/ask", json={"question": "q"})

        second_after_peer_shutdown = second_client.post(
            "/ask", json={"question": "q"}
        )

    assert first_response.json()["answer"] == "first app"
    assert second_response.json()["answer"] == "second app"
    assert second_after_peer_shutdown.json()["answer"] == "second app"


def test_direct_get_graph_call_keeps_the_legacy_compile_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fin.host.application.composition as composition_module
    from fin.host import http as http_module

    legacy_graph = object()
    monkeypatch.setattr(
        composition_module, "compile_for_http", lambda **_kwargs: legacy_graph
    )

    assert http_module.get_graph() is legacy_graph


def test_application_shutdown_keeps_a_peer_resume_worker_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each app starts and stops the resume worker bound to its own graph."""
    from fin.host import workspace_http

    monkeypatch.setattr(workspace_http, "_attachment_resume_worker", None)
    first_graph = object()
    second_graph = object()
    first_started = threading.Event()
    second_started = threading.Event()
    second_repeated = threading.Event()
    seen: list[tuple[Any, Any]] = []

    def _sweep(
        graph: Any,
        _store: Any,
        _service: Any,
        control: Any,
        *,
        workspace_root: Any,
        receipts: Any,
    ) -> None:
        seen.append((graph, control))
        if graph is first_graph:
            first_started.set()
        if graph is second_graph:
            second_started.set()
            if sum(item[0] is second_graph for item in seen) > 1:
                second_repeated.set()

    monkeypatch.setattr(workspace_http, "_resume_attachment_waits_once", _sweep)

    def _steps(graph: Any) -> StartupSteps:
        events: list[str] = []
        recording = _recording_steps(events, graph=graph)
        return replace(
            recording,
            start_attachment_resume_worker=lambda **kwargs: (
                workspace_http.start_application_attachment_resume_worker(
                    **kwargs, check_interval_seconds=0.01
                )
            ),
            stop_attachment_resume_worker=workspace_http.stop_attachment_resume_worker,
        )

    first = ApplicationResources(build_steps=lambda: _steps(first_graph))
    second = ApplicationResources(build_steps=lambda: _steps(second_graph))

    first.startup()
    try:
        second.startup()
        try:
            assert first_started.wait(5)
            assert second_started.wait(5)

            first.shutdown()

            assert second_repeated.wait(5)
            assert any(graph is second_graph for graph, _control in seen)
        finally:
            second.shutdown()
    finally:
        first.shutdown()


def _write_spa_dist(root: Path, marker: str) -> Path:
    """A throwaway SPA build whose every served byte says which one it
    is, so an app that answered out of the OTHER app's dist is a failed
    assertion rather than a coincidence."""
    dist = root / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text(f"// {marker}\n", encoding="utf-8")
    (dist / "index.html").write_text(f"<html>{marker}</html>\n", encoding="utf-8")
    return dist


def test_two_applications_each_mount_their_own_spa(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The SPA mount is per app: a module-global guard left the second
    app in one process with neither ``/assets`` nor the catch-all, and
    no warning either."""
    from fin.host import http as http_module
    from fin.host.http import mount_spa

    # WHY the source file is moved: ``create_app`` mounts the REAL
    # ``web/dist`` on every machine where the SPA has been built, and
    # that mount is per app too — so there both explicit mounts below
    # are no-ops and ``/assets/app.js`` answers 404 out of the guard
    # instead of out of the dists written here. With no build (CI) the
    # case passed for the wrong reason: nothing had mounted yet.
    # ``mount_spa`` resolves its default as ``parents[3] / "web" /
    # "dist"`` of its OWN source, so a source path under this case's
    # ``tmp_path`` — a checkout with no ``web/dist`` — leaves
    # ``create_app`` mounting nothing and makes each mount below the
    # app's FIRST. ``web/dist`` itself stays out of this case on
    # purpose: it is a Vite build artifact, so a case that reads it
    # tests whether this machine ran ``npm run build`` — the same
    # reason ``tests/host/test_workspace_http.py`` mounts a ``tmp_path``
    # dist.
    monkeypatch.setattr(
        http_module,
        "__file__",
        str(tmp_path / "checkout" / "src" / "fin" / "host" / "http.py"),
    )

    first = _recording_app([], graph=object())
    second = _recording_app([], graph=object())

    # CHECKED, not assumed: if the default lookup ever stops reading
    # ``__file__``, this case would depend on the ambient build again
    # and its mounts would be no-ops. A SPA-less 404 is the
    # precondition that makes the rest of the case mean anything.
    assert TestClient(first).get("/t/some-thread").status_code == 404
    assert TestClient(second).get("/t/some-thread").status_code == 404

    for app, marker in ((first, "spa-one"), (second, "spa-two")):
        mount_spa(app, dist_path=str(_write_spa_dist(tmp_path / marker, marker)))

    for app, marker in ((first, "spa-one"), (second, "spa-two")):
        client = TestClient(app)
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/app.js").text == f"// {marker}\n"
        assert client.get("/t/some-thread").status_code == 200
        assert marker in client.get("/t/some-thread").text


def test_mounting_the_spa_twice_on_one_app_is_still_a_no_op(
    tmp_path: Path,
) -> None:
    """The documented idempotence is per app: a second call must not
    reach Starlette, which rejects a duplicate path."""
    from fin.host.http import mount_spa

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>spa</html>\n", encoding="utf-8")
    app = FastAPI()

    mount_spa(app, dist_path=str(dist))
    mount_spa(app, dist_path=str(dist))

    client = TestClient(app)
    assert client.get("/t/some-thread").status_code == 200


# ----- the router and the auto-resumer share the app's registry --------


def test_the_mounted_router_and_the_attachment_resumer_share_one_run_control() -> None:
    from fin.host.http import get_thread_store

    events: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    store = InMemoryThreadStore()
    app = _recording_app(events, seen=seen)
    app.dependency_overrides[get_thread_store] = lambda: store
    control = app.state.fin_resources.run_control

    with TestClient(app) as client:
        thread_id = store.create_thread(title="background").id
        # Registered on the APP's registry, which is what the auto-resumer
        # does — so a 200 here proves the mounted router reads that same
        # object rather than a registry of its own.
        control.begin(thread_id, "run-bg")
        response = client.post(f"/api/threads/{thread_id}/stop")

    assert response.status_code == 200, response.text
    assert response.json() == {"stopped": True, "run_id": "run-bg"}
    resume_kwargs = seen["start_attachment_resume_worker"]["kwargs"]
    assert resume_kwargs["run_control"] is control


# ----- the application owns one run-call supervisor --------------------


def test_the_compile_receives_the_applications_own_supervisor() -> None:
    """The supervisor is handed IN, so the graph's cached compile and the
    shutdown that closes it are the same object.

    A supervisor minted inside the compile would be unreachable from
    shutdown, and a graph compiled against one supervisor while the
    application closes another is how in-flight model calls survive the
    server.
    """
    seen: dict[str, dict[str, Any]] = {}
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps([], seen=seen)
    )

    resources.startup()
    try:
        supervisor = seen["compile_runtime"]["kwargs"]["run_call_supervisor"]
        assert supervisor is resources.run_call_supervisor
    finally:
        resources.shutdown()


def test_a_repeated_lifespan_compiles_against_a_supervisor_that_is_still_live() -> None:
    """The SECOND boot gets its own supervisor, and it is a usable one.

    Shutdown closes the supervisor the first boot ran under, so reusing
    it would compile a graph whose every model call is refused as
    ``run-call supervisor is closed``. What must not vary is that the
    compile is handed the owner it can be closed by, every time.
    """
    events: list[str] = []
    seen: dict[str, dict[str, Any]] = {}
    app = _recording_app(events, seen=seen)
    resources = app.state.fin_resources

    with TestClient(app):
        pass
    first = seen["compile_runtime"]["kwargs"]["run_call_supervisor"]
    with TestClient(app):
        second = seen["compile_runtime"]["kwargs"]["run_call_supervisor"]
        assert second.closed is False

    assert first is not second
    assert second is resources.run_call_supervisor
    assert first.closed is True


def test_shutdown_stops_every_live_run_before_the_runtime_releases() -> None:
    """A run still in flight must be told to stop while the runtime that
    owns its graph is still up.

    Stopping AFTER the release would flip a flag nobody is listening to
    any more, and the turn would sit mid-model-call until its budget.
    """
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(events)
    )
    resources.startup()
    token = resources.run_control.begin("t-1", "run-1")
    events.clear()

    resources.shutdown()

    assert token.is_set() is True
    assert token.reason == "application_shutdown"
    assert events == [
        "stop_receipt_export_worker",
        "stop_invalidation_drain_worker",
        "stop_attachment_resume_worker",
        "stop_attachment_prep_worker",
        "stop_ingest_worker",
        "release_runtime",
    ], events


def test_two_applications_do_not_share_a_run_call_supervisor() -> None:
    first = ApplicationResources(build_steps=lambda: _recording_steps([]))
    second = ApplicationResources(build_steps=lambda: _recording_steps([]))

    assert first.run_call_supervisor is not second.run_call_supervisor


# ----- a failure at any stage unwinds what ran before it ----------------


@pytest.mark.parametrize("fail_at", _BOOT_STAGES)
def test_a_failure_at_each_stage_unwinds_in_reverse_then_reraises(
    fail_at: str,
) -> None:
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(events, fail_at=fail_at)
    )

    with pytest.raises(_StageFailure):
        resources.startup()

    assert events == [
        *_stages_through(fail_at),
        *_owed_releases(events, fail_at),
    ], events
    assert resources.running is False
    assert resources.graph is None


def test_a_failure_at_the_last_stage_releases_everything_exactly_once() -> None:
    events: list[str] = []
    last = _BOOT_STAGES[-1]
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(events, fail_at=last)
    )

    with pytest.raises(_StageFailure):
        resources.startup()

    released = [event for event in events if event in _RELEASE_NAMES]
    assert released == _owed_releases(events, last)
    assert len(released) == len(set(released)), events


def test_every_worker_seam_has_the_stop_seam_that_releases_it() -> None:
    """``StartupSteps`` states the boot order by field order.

    A ``start_*`` field with no ``stop_*`` twin is an acquisition this
    contract cannot express a release for, so the pair is checked here
    rather than discovered when a shutdown cannot stop a thread.
    """
    starts = {name for name in _STEP_NAMES if name.startswith("start_")}
    stops = {name for name in _STEP_NAMES if name.startswith("stop_")}

    assert stops == {name.replace("start_", "stop_", 1) for name in starts}
    assert set(_RELEASE_FOR_STEP) == starts | {"compile_runtime"}


# ----- a repeated lifespan leaks nothing --------------------------------


def test_entering_and_exiting_the_lifespan_twice_leaks_nothing() -> None:
    events: list[str] = []
    app = _recording_app(events)
    threads_before = threading.active_count()

    with TestClient(app):
        pass
    with TestClient(app):
        pass

    for release in _RELEASE_NAMES:
        assert events.count(release) == 2, events
    assert threading.active_count() == threads_before
    assert app.state.fin_resources.running is False


# ----- the refusals that must precede every acquisition -----------------


def _resources_over_gates(events: list[str], **gates: Any) -> ApplicationResources:
    """Resources whose named seams run the DEPLOYED gate behind a recorder.

    Every other seam stays inert, so the only thing that can refuse this
    boot is the gate under test — and it still shows up in ``events``, so
    the assertion can say which position it refused at.
    """
    return ApplicationResources(
        build_steps=lambda: _recording_steps(events, gates=gates)
    )


def test_an_absent_checkpoint_url_is_refused_before_anything_is_acquired(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fin.host.compile import MissingCheckpointURLError
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    monkeypatch.delenv("FIN_CHECKPOINT_URL", raising=False)
    monkeypatch.delenv("FIN_SCRIPTED_MODEL", raising=False)
    events: list[str] = []
    resources = _resources_over_gates(
        events, require_checkpoint_url=composition_module._require_checkpoint_url
    )

    with pytest.raises(MissingCheckpointURLError):
        resources.startup()

    assert events == [
        "require_single_process",
        "apply_operator_settings",
        "receipt_export_config",
        "attachment_budgets",
        "require_checkpoint_url",
    ]


def test_invalid_receipt_export_configuration_refuses_before_acquisition() -> None:
    from fin.host.receipt_export import (
        ReceiptExportConfigError,
        load_receipt_export_config,
    )

    events: list[str] = []
    steps = _recording_steps(
        events,
        gates={
            "receipt_export_config": lambda: load_receipt_export_config(
                {"OTEL_TRACES_EXPORTER": "not-a-trace-exporter"}
            )
        },
    )
    resources = ApplicationResources(build_steps=lambda: steps)

    with pytest.raises(ReceiptExportConfigError):
        resources.startup()

    assert events == [
        "require_single_process",
        "apply_operator_settings",
        "receipt_export_config",
    ]
    assert resources.running is False
    assert resources.graph is None
    assert resources.running is False


def test_the_boot_publishes_the_resolved_export_selection_once() -> None:
    """One configuration authority: the export worker and the span view
    read the selection this boot resolved, not the environment."""
    from fin.host.receipt_export import load_receipt_export_config

    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "console"})
    assert config is not None
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(
            events, gates={"receipt_export_config": lambda: config}
        )
    )
    assert resources.receipt_export_config is None

    resources.startup()
    try:
        assert events.count("receipt_export_config") == 1
        assert resources.receipt_export_config is config
    finally:
        resources.shutdown()

    assert resources.receipt_export_config is None


def test_a_refused_boot_publishes_no_export_selection() -> None:
    """A boot that gave up owns no exporter, so the view cannot claim one."""
    from fin.host.receipt_export import load_receipt_export_config

    config = load_receipt_export_config({"OTEL_TRACES_EXPORTER": "console"})
    assert config is not None
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(
            events,
            fail_at="attachment_budgets",
            gates={"receipt_export_config": lambda: config},
        )
    )

    with pytest.raises(_StageFailure):
        resources.startup()

    assert resources.running is False
    assert resources.receipt_export_config is None


@pytest.mark.parametrize(
    "value",
    ["", "0", "-1", "1.5", "1_000", "１０", "nan", "inf", "abc"],
)
def test_an_unusable_attachment_bound_is_refused_before_anything_is_acquired(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    monkeypatch.setenv("FIN_CHECKPOINT_URL", "postgresql://placeholder")
    monkeypatch.setenv("FIN_ATTACHMENT_MAX_BYTES", value)
    # The deployed bounds are read once per process; a case that wants
    # this env must not be answered from an earlier read.
    monkeypatch.setattr(
        composition_module, "_http_attachment_budgets_singleton", None, raising=False
    )
    events: list[str] = []
    resources = _resources_over_gates(
        events,
        require_checkpoint_url=composition_module._require_checkpoint_url,
        attachment_budgets=composition_module.attachment_budgets,
    )

    with pytest.raises(AttachmentBudgetConfigError):
        resources.startup()

    assert events == [
        "require_single_process",
        "apply_operator_settings",
        "receipt_export_config",
        "attachment_budgets",
    ]
    assert resources.running is False


# ----- one server process, or none -------------------------------------

_NO_FLAG = ["fin-serve"]


def test_an_undeclared_or_single_deployment_is_one_process() -> None:
    declared_one = {WORKER_COUNT_ENV_VAR: "1"}
    assert declared_process_count(environ={}, argv=_NO_FLAG)[0] == "1"
    assert declared_process_count(
        None, environ=declared_one, argv=_NO_FLAG
    )[0] == "1"
    assert declared_process_count(1, environ={}, argv=_NO_FLAG)[0] == "1"
    require_single_process(1, environ={}, argv=_NO_FLAG)
    require_single_process(None, environ=declared_one, argv=_NO_FLAG)


def test_an_unreadable_declared_count_is_refused_as_written() -> None:
    """A value this deployment cannot state is a refusal, not a guess —
    and the refusal names what the operator typed, not a sentinel."""
    undeclared = {WORKER_COUNT_ENV_VAR: "two"}

    with pytest.raises(UnsupportedProcessCountError) as exc:
        require_single_process(None, environ=undeclared, argv=_NO_FLAG)

    assert exc.value.declared == "two"
    assert "two" in str(exc.value)
    assert "-1" not in str(exc.value)


@pytest.mark.parametrize("declared", [2, 4, 0, -1])
def test_a_declared_count_other_than_one_is_refused(declared: int) -> None:
    """The refusal names the value, and where it was read from — an
    injected count is not an environment declaration."""
    with pytest.raises(UnsupportedProcessCountError) as exc:
        require_single_process(declared, environ={}, argv=_NO_FLAG)

    assert exc.value.declared == str(declared)
    assert WORKER_COUNT_ENV_VAR not in str(exc.value)
    assert str(declared) in str(exc.value)


# The real deployment spelling. uvicorn reads ``WEB_CONCURRENCY`` but
# never writes it, so ``uvicorn --workers 4 fin.host.http:app`` starts its
# children with the key UNSET: the count is only on the command line,
# and a spawned child inherits it on ``sys.argv``.
@pytest.mark.parametrize(
    "argv",
    [
        ["uvicorn", "--workers", "4", "fin.host.http:app"],
        ["uvicorn", "--workers=4", "fin.host.http:app"],
        ["uvicorn", "--port", "8000", "--workers", "4", "fin.host.http:app"],
    ],
)
def test_the_uvicorn_workers_flag_shape_is_refused(argv: list[str]) -> None:
    with pytest.raises(UnsupportedProcessCountError) as exc:
        require_single_process(None, environ={}, argv=argv)

    assert exc.value.declared == "4"


def test_the_refused_count_is_the_count_uvicorn_itself_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What is refused has to be what would actually run. uvicorn hands
    ``--workers`` straight to ``Config(workers=...)`` and reads
    ``WEB_CONCURRENCY`` into that same field, so both declarations are
    compared against the Config this server would really build."""
    from uvicorn.config import Config

    from_flag, _source = declared_process_count(
        None,
        environ={},
        argv=["uvicorn", "--workers", "4", "fin.host.http:app"],
    )
    monkeypatch.setenv(WORKER_COUNT_ENV_VAR, "4")
    from_env, _source = declared_process_count()

    assert int(from_flag) == Config("fin.host.http:app", workers=4).workers
    assert int(from_env) == Config("fin.host.http:app").workers == 4


def test_a_spawned_worker_child_can_still_see_the_flag_it_was_forked_from(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The premise the flag shape rests on: the child uvicorn spawns for
    ``--workers`` carries the parent's argv, because that is how it is
    handed its socket and its config."""
    import multiprocessing.spawn

    monkeypatch.setattr(
        sys, "argv", ["uvicorn", "--workers", "4", "fin.host.http:app"]
    )

    preparation = multiprocessing.spawn.get_preparation_data("fin-child")

    assert preparation["sys_argv"] == sys.argv
    assert declared_process_count(None, environ={})[0] == "4"


def test_a_multi_process_deployment_is_refused_before_the_boot_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed boot itself refuses, and it refuses before the first
    seam that would have acquired anything."""
    from fin.host.http import create_app

    acquired: list[str] = []
    _stub_the_deployed_seams_that_open(
        monkeypatch,
        compile_runtime=lambda: RuntimeHandle(object()),
        acquired=acquired,
    )
    app = create_app(process_count=2)

    with pytest.raises(UnsupportedProcessCountError) as exc:
        with TestClient(app):
            pass

    assert exc.value.declared == "2"
    assert acquired == []
    assert app.state.fin_resources.running is False


def test_the_deployed_boot_refuses_a_declared_worker_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``WEB_CONCURRENCY`` is the deployment's own spelling, and the boot
    reads it through the deployed refusal before acquiring anything."""
    from fin.host.http import create_app

    monkeypatch.setenv(WORKER_COUNT_ENV_VAR, "4")
    acquired: list[str] = []
    _stub_the_deployed_seams_that_open(
        monkeypatch,
        compile_runtime=lambda: RuntimeHandle(object()),
        acquired=acquired,
    )
    app = create_app()

    with pytest.raises(UnsupportedProcessCountError) as exc:
        with TestClient(app):
            pass

    assert exc.value.declared == "4"
    assert exc.value.source == WORKER_COUNT_ENV_VAR
    assert acquired == []


def test_importing_the_module_under_a_hostile_declaration_does_not_raise() -> None:
    """``fin.host.http`` is imported from request paths, so an import that
    refuses would surface inside a request rather than at boot."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"FIN_CHECKPOINT_URL", "FIN_TEST_DSN", "FIN_SCRIPTED_MODEL"}
    }
    env[WORKER_COUNT_ENV_VAR] = "two"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.argv = ['uvicorn', '--workers', '4', "
            "'fin.host.http:app']; import fin.host.http as m; print(type(m.app).__name__)",
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == "FastAPI"


# ----- the deployed spellings ------------------------------------------


def test_the_module_level_app_is_still_a_fastapi() -> None:
    from fin.host.http import app

    assert isinstance(app, FastAPI)
    assert app.state.fin_resources.running is False


def test_dependency_overrides_still_answer_a_real_request() -> None:
    from fin.host.http import app, get_graph

    class _GraphStandIn:
        """A ``done`` run with no model behind it. What this case asks is
        whether the override reaches the route as its graph."""

        def __init__(self) -> None:
            self.configs: list[Any] = []

        def invoke(self, state: Any, config: Any) -> dict[str, Any]:
            self.configs.append(config)
            return {
                "status": "done",
                "answer": "overridden graph answered",
                "citations": [],
            }

    graph = _GraphStandIn()
    app.dependency_overrides[get_graph] = lambda: graph
    try:
        # Deliberately not a context manager: this asserts the override
        # answers a real route without booting the production runtime.
        response = TestClient(app).post("/ask", json={"question": "q"})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "done"
    assert response.json()["answer"] == "overridden graph answered"
    assert len(graph.configs) == 1


def test_serve_is_still_the_fin_serve_entry_point() -> None:
    from fin.host.http import serve

    scripts = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["scripts"]

    assert callable(serve)
    assert scripts["fin-serve"] == "fin.host.http:serve"


# ----- the handle is closeable without knowing what is behind it --------


def test_runtime_handle_close_is_idempotent() -> None:
    released: list[str] = []
    handle = RuntimeHandle(
        object(), release=lambda: released.append("release")
    )
    assert handle.closed is False

    handle.close()
    handle.close()

    assert released == ["release"]
    assert handle.closed is True


def test_runtime_handle_releases_a_stack_behind_it_without_naming_it() -> None:
    """What the stack holds — a checkpointer context manager, a pool,
    anything — is released through the handle alone."""

    class _SaverStandIn:
        """Stands in for the checkpointer the compile entered."""

        def __init__(self) -> None:
            self.entered = 0
            self.exited = 0

        def __enter__(self) -> "_SaverStandIn":
            self.entered += 1
            return self

        def __exit__(self, *exc: Any) -> None:
            self.exited += 1

    saver = _SaverStandIn()
    owned = ExitStack()
    owned.enter_context(saver)
    handle = RuntimeHandle(object(), release=owned.close)

    handle.close()
    handle.close()

    assert saver.entered == 1
    assert saver.exited == 1


def test_a_failing_release_still_runs_the_handle_s_own_after_close() -> None:
    """``on_close`` is what keeps a dependency cache from answering with a
    graph whose checkpointer this handle already dropped, so a release
    that raises cannot be allowed to skip it."""
    ran: list[str] = []

    def _failing_release() -> None:
        ran.append("release")
        raise RuntimeError("checkpointer close failed")

    handle = RuntimeHandle(
        object(),
        release=_failing_release,
        on_close=lambda: ran.append("on_close"),
    )

    with pytest.raises(RuntimeError, match="checkpointer close failed"):
        handle.close()

    assert ran == ["release", "on_close"]
    assert handle.closed is True


# ----- the deployed boot owns what it compiled ---------------------------


def test_the_deployed_boot_releases_the_runtime_it_compiled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The release reaches shutdown through the DEPLOYED path.

    Everything else about the boot is inert; the runtime is not. A
    release the owner forgets cannot be seen by a case that injects its
    own recording handle, so what is asserted here is the connection
    the handle closed, counted at the only place that knows.
    """
    from fin.host.http import create_app

    acquired = _ObservableConnection()
    owned = ExitStack()
    owned.enter_context(acquired)
    handle = RuntimeHandle(object(), release=owned.close)
    _stub_the_deployed_seams_that_open(
        monkeypatch, compile_runtime=lambda: handle
    )
    app = create_app()

    with TestClient(app):
        assert acquired.exits == 0

    assert acquired.exits == 1
    assert handle.closed is True


def test_a_repeated_lifespan_over_a_real_worker_releases_and_leaks_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Two boots of the deployed composition: a real worker thread, a real
    handle per boot, and the connection count each handle closed."""
    from fin.corpus.object_store import InMemoryObjectStore
    from fin.host import workspace_http
    from fin.host.attachment.attachment_job_store import InMemoryThreadAttachmentJobStore
    from fin.host.attachment.attachment_prepare import AttachmentPreparationService
    from fin.host.attachment.attachment_store import InMemoryAttachmentStore
    from fin.host.http import create_app

    service = AttachmentPreparationService(
        store=InMemoryAttachmentStore(InMemoryThreadStore()),
        objects=InMemoryObjectStore(),
        jobs=InMemoryThreadAttachmentJobStore(),
    )
    acquired: list[_ObservableConnection] = []
    handles: list[RuntimeHandle] = []

    def _compile() -> RuntimeHandle:
        connection = _ObservableConnection()
        owned = ExitStack()
        owned.enter_context(connection)
        acquired.append(connection)
        compiled = RuntimeHandle(object(), release=owned.close)
        handles.append(compiled)
        return compiled

    # The drain is a per-process singleton, so this case's worker must
    # not be handed to the rest of the session.
    monkeypatch.setattr(workspace_http, "_attachment_prep_worker", None)
    _stub_the_deployed_seams_that_open(
        monkeypatch,
        compile_runtime=_compile,
        prep_worker=service,
        workspace_root=tmp_path,
    )
    app = create_app()
    threads_before = threading.active_count()

    with TestClient(app):
        during_boots = threading.active_count()
    with TestClient(app):
        assert threading.active_count() == during_boots

    assert [handle.closed for handle in handles] == [True, True]
    assert [connection.exits for connection in acquired] == [1, 1]
    assert threading.active_count() == threads_before
    assert app.state.fin_resources.running is False


def test_identity_runtime_closes_only_its_own_evidence_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each identity-bearing compile owns and drains its registry once."""
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    class _Identity:
        def child_storage_env(self) -> dict[str, str]:
            return {}

        def resolver(self) -> object:
            return object()

    class _Registry:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            self.close_calls = 0

        def close_all(self) -> None:
            self.close_calls += 1

    registries: list[_Registry] = []
    compiled_releases: list[int] = []

    def _registry(*args: Any, **kwargs: Any) -> _Registry:
        registry = _Registry(*args, **kwargs)
        registries.append(registry)
        return registry

    def _compile_product(**kwargs: Any) -> RuntimeHandle:
        registry = kwargs["evidence"].sessions
        index = registries.index(registry)
        return RuntimeHandle(object(), release=lambda: compiled_releases.append(index))

    monkeypatch.setattr(
        composition_module, "load_structured_evidence_identity", lambda: _Identity()
    )
    monkeypatch.setattr(composition_module, "EvidenceSessionRegistry", _registry)
    monkeypatch.setattr(composition_module, "_compile_product_handle", _compile_product)

    first = composition_module._compile_http_runtime()
    second = composition_module._compile_http_runtime()
    first.close()
    first.close()

    assert [registry.close_calls for registry in registries] == [1, 0]
    assert compiled_releases == [0]

    second.close()
    assert [registry.close_calls for registry in registries] == [1, 1]
    assert compiled_releases == [0, 1]


def test_identity_compile_failure_drains_its_evidence_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    class _Identity:
        def child_storage_env(self) -> dict[str, str]:
            return {}

        def resolver(self) -> object:
            return object()

    class _Registry:
        close_calls = 0

        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def close_all(self) -> None:
            self.close_calls += 1

    registries: list[_Registry] = []

    def _registry(*args: Any, **kwargs: Any) -> _Registry:
        registry = _Registry(*args, **kwargs)
        registries.append(registry)
        return registry

    def _compile_product(**_kwargs: Any) -> RuntimeHandle:
        raise RuntimeError("graph compile failed")

    monkeypatch.setattr(
        composition_module, "load_structured_evidence_identity", lambda: _Identity()
    )
    monkeypatch.setattr(composition_module, "EvidenceSessionRegistry", _registry)
    monkeypatch.setattr(composition_module, "_compile_product_handle", _compile_product)

    with pytest.raises(RuntimeError, match="graph compile failed"):
        composition_module._compile_http_runtime()

    assert len(registries) == 1
    assert registries[0].close_calls == 1


def test_a_failing_release_neither_skips_the_others_nor_hides_the_boot_failure() -> None:
    """A checkpointer that will not close must not leave the workers the
    boot started running, and must not become the reported cause: the
    operator's fix is the boot failure, not the unwind."""
    events: list[str] = []

    def _failing_release() -> None:
        events.append("stop_attachment_resume_worker")
        raise RuntimeError("unwind failed")

    steps = replace(
        _recording_steps(events, fail_at="start_invalidation_drain_worker"),
        stop_attachment_resume_worker=_failing_release,
    )
    resources = ApplicationResources(build_steps=lambda: steps)

    with pytest.raises(_StageFailure):
        resources.startup()

    assert [event for event in events if event in _RELEASE_NAMES] == [
        "stop_attachment_resume_worker",
        "stop_attachment_prep_worker",
        "stop_ingest_worker",
        "release_runtime",
    ], events
    assert resources.running is False
    assert resources.graph is None


# ----- every acquisition has exactly one release -------------------------


def test_the_deployed_composition_releases_everything_it_declares_acquired() -> None:
    """The deployed boot's own statement about each seam, driven end to end.

    ``ownership`` is the composition saying what every call acquires; this
    runs that composition and checks the statement — each acquired seam
    released exactly once, innermost first, and nothing the composition
    called a probe or a process singleton produced a release.
    """
    from fin.host.http import startup_steps

    deployed = startup_steps()
    events: list[str] = []
    resources = ApplicationResources(
        build_steps=lambda: _recording_steps(
            events, ownership=deployed.ownership
        )
    )

    resources.startup()
    resources.shutdown()

    declared = {
        name
        for name, verdict in deployed.ownership.items()
        if verdict == SEAM_ACQUIRED
    }
    acquired_in_order = [name for name in events if name in _RELEASE_FOR_STEP]
    assert set(acquired_in_order) == declared
    assert [event for event in events if event in _RELEASE_NAMES] == [
        _RELEASE_FOR_STEP[name] for name in reversed(acquired_in_order)
    ], events


def test_every_seam_the_deployed_boot_calls_says_what_it_acquires() -> None:
    """The seam set is read off the dataclass, so a composition that grows
    a seam has to say what that seam acquires before it can boot — which
    is what stops an unreleased acquisition from arriving unannounced."""
    from fin.host.http import startup_steps

    deployed = startup_steps()
    called = {name for name in _STEP_NAMES if not name.startswith("stop_")}

    assert set(deployed.ownership) == called
    assert set(deployed.ownership.values()) <= {
        SEAM_ACQUIRED,
        SEAM_PROCESS,
        SEAM_PROBE,
    }


def test_the_deployed_export_stop_seam_ends_the_exporter_it_started(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The declared ``stop_*`` seam must be a release, not a placeholder.

    The owner runs the callable the START seam returned, so a no-op stop
    seam is dead code that still reads like a contract: it promises the
    exporter's two daemon threads can be ended and proves nothing. What
    the start seam handed back is what the stop seam ends.
    """
    import fin.host.application.composition as composition_module
    import fin.host.http as http_module

    released: list[str] = []
    monkeypatch.setattr(
        composition_module,
        "start_application_receipt_export_worker",
        lambda **_kwargs: (lambda: released.append("released"))(),
    )
    monkeypatch.setattr(composition_module, "_receipt_export_release", None)

    stop = http_module.startup_steps().stop_receipt_export_worker
    http_module.startup_steps().start_receipt_export_worker(store=object(), config=None)
    stop()
    stop()

    assert released == ["released"]


def test_the_ingest_storage_preflight_is_declared_process_owned() -> None:
    """Building the ingest ports opens the durable connections the ingest
    service then uses, and the only reset of that snapshot is a test seam
    — so the PROCESS holds them. Saying so here is what keeps a later
    boot from inventing a close nobody owns."""
    from fin.host.http import startup_steps

    deployed = startup_steps()

    assert deployed.ownership["preflight_ingest_storage"] == SEAM_PROCESS

