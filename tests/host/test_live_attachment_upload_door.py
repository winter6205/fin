"""The raw-upload door against real fin storage and a real process.

Why this file exists
--------------------
``tests/host/test_thread_attach.py`` proves the upload door's contract on
``InMemoryAttachmentStore`` + ``InMemoryObjectStore`` inside the test
process. That is the right shape for the boundary matrix, but it cannot
answer the two questions this door's acceptance turns on:

* does a durable ``uploaded`` row reach a *different* process that never
  saw the upload, and does that process's recovery leave it alone?
* is the byte cap the door applies the one the deployment states, or a
  number the route carried?

So this module drives the real product composition —
:func:`fin.host.workspace_http._build_default_attachment_service` for the
stores and :func:`fin.host.attachment_budgets.from_environ` for the
bounds, both called as production calls them — against the Postgres at
``FIN_CHECKPOINT_URL``, over two harnesses:

* ``door`` — ``TestClient`` over the real ``fin.host.http.app``. Same
  process as the test, which is what lets the byte store be observed
  directly: the "no partial object" and "no units, no postings" clauses
  are read off the real adapters rather than inferred.
* ``live_server`` — a separate ``uvicorn`` process serving the same real
  app on a real socket, so a 200/413 and the rows behind it are crossed
  over a process boundary and re-read over a second SQL connection.

What is real here, and what is not
----------------------------------
Real: the FastAPI app and route, the bounded body reader, the deployed
``FIN_ATTACHMENT_*`` bounds read from ``.env.example``,
``PostgresThreadStore`` and ``PostgresAttachmentStore`` on a live
Postgres, the object-store adapter the product selects from the
environment, and the committed gold payloads under ``data/corpus/``.

Not real: only ``get_graph``. The upload door never reaches the graph,
and a live graph means a live model, which this change does not involve
and which ``AGENTS.md`` keeps out of the test path.

The byte adapter is not hand-picked: :func:`create_object_store` raises
``ObjectStoreNotConfiguredError`` with no ``FIN_OBJECT_STORE_URL`` /
``FIN_OBJECT_STORE_BUCKET``, and the product's own builder then takes its
documented degradation to the process-local adapter and logs it. The original
run used that degraded path; the configured S3 path now has a separate
cross-process read-back case recorded in
``docs/validation/2026-09-26-g4-t2b-raw-upload-send-gate.md``.

Opt-in
------
``pytestmark = pytest.mark.mcp`` keeps this out of the default hermetic
run (``tests/host/conftest.py`` deselects it). Run it deliberately:

    FIN_CHECKPOINT_URL=… uv run --no-sync pytest -q -m mcp \\
        tests/host/test_live_attachment_upload_door.py

It also follows the repo's ``FIN_CHECKPOINT_URL`` skip convention
(``tests/host/test_attachment_store.py``,
``tests/host/test_thread_attachment_durable_postgres.py``). The mark
already requires a deliberate opt-in, so a skip here names a missing
prerequisite rather than standing as a green pass; the validation record
logs it as blocked evidence when it happens.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from fin.host.attachment_budgets import _ENV_BOUNDS as _PRODUCT_ENV_BOUNDS

REPO_ROOT = Path(__file__).resolve().parents[2]
GOLD_DIR = REPO_ROOT / "data" / "corpus" / "g4_attachment_gold"
PDF_DIR = REPO_ROOT / "data" / "corpus" / "pdf"

MIXED_PDF = GOLD_DIR / "g4-mixed-chart-001.pdf"
SCANNED_PDF = GOLD_DIR / "g4-scanned-001.pdf"
CHART_PNG = GOLD_DIR / "g4-image-chart-001.png"
MSFT_Q1_PDF = PDF_DIR / "2023_Q1_MSFT.pdf"

# The size that makes the byte-cap claim non-trivial. The route constant
# this door retired; pinned here so the record can show a
# real filing that the OLD cap rejected and the DEPLOYED bound accepts.
RETIRED_ROUTE_CAP_BYTES = 2 * 1024 * 1024

# Read from the product's own table: a fifth bound would otherwise be
# undeclared here and the boot gate below would silently test four of five.
BOUND_KEYS = tuple(key for key, _ in _PRODUCT_ENV_BOUNDS)

pytestmark = pytest.mark.mcp


# ----- preconditions -----


@pytest.fixture(scope="module")
def deployed_bounds() -> dict[str, str]:
    """The four bounds exactly as ``.env.example`` ships them.

    Read from the template rather than restated here, so this module can
    never bless a number the deployment does not carry.
    """
    from fin.host.operator_env import parse_env_file

    values = parse_env_file((REPO_ROOT / ".env.example").read_text())
    missing = [key for key in BOUND_KEYS if key not in values]
    assert not missing, f".env.example lost {missing}"
    return {key: values[key] for key in BOUND_KEYS}


@pytest.fixture(scope="module")
def postgres_url(deployed_bounds: dict[str, str]) -> str:
    """The live DSN, or the repo's named skip."""
    url = (os.environ.get("FIN_CHECKPOINT_URL") or "").strip()
    if not url:
        pytest.skip(
            "FIN_CHECKPOINT_URL is not set; this module is the real-storage "
            "evidence and cannot run against an in-memory stand-in"
        )
    return url


@pytest.fixture
def real_s3_object_store() -> None:
    """Require endpoint and bucket configuration without exposing values."""
    if not _s3_object_store_configured():
        pytest.skip(
            "FIN_OBJECT_STORE_URL/FIN_OBJECT_STORE_ENDPOINT and "
            "FIN_OBJECT_STORE_BUCKET are not configured; durable S3 "
            "read-back was not run"
        )


@pytest.fixture
def budgets(
    monkeypatch: pytest.MonkeyPatch, deployed_bounds: dict[str, str]
) -> Any:
    """The deployed bound set, read the way ``serve()`` reads it.

    ``get_attachment_budgets`` caches one set per process, so a test that
    deploys an environ has to clear the cache alongside it — the same pin
    ``tests/host/conftest.py`` applies to its boot fixture.
    """
    import fin.host.http as http_module
    from fin.host.attachment_budgets import from_environ

    for key, value in deployed_bounds.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(http_module, "_http_attachment_budgets_singleton", None)
    return from_environ(environ=os.environ)


# ----- real-storage probes (a second SQL connection, never the app's) -----


def _sql_rows(url: str, statement: str, params: tuple[Any, ...] = ()) -> list[Any]:
    import psycopg

    with psycopg.connect(url) as conn:
        return conn.execute(statement, params).fetchall()


def _attachment_row(url: str, attachment_id: str) -> dict[str, Any] | None:
    rows = _sql_rows(
        url,
        "SELECT attachment_id, thread_id, filename, size_bytes, sha256, "
        "object_key, status, version, unit_count, indexed, failure_reason "
        "FROM fin_thread_attachment WHERE attachment_id = %s",
        (attachment_id,),
    )
    if not rows:
        return None
    keys = (
        "attachment_id",
        "thread_id",
        "filename",
        "size_bytes",
        "sha256",
        "object_key",
        "status",
        "version",
        "unit_count",
        "indexed",
        "failure_reason",
    )
    return dict(zip(keys, rows[0]))


def _derived_counts(url: str, attachment_id: str) -> tuple[int, int]:
    """(frozen unit rows, index postings) for one attachment, across all versions."""
    units = _sql_rows(
        url,
        "SELECT count(*) FROM fin_thread_attachment_unit WHERE attachment_id = %s",
        (attachment_id,),
    )[0][0]
    postings = _sql_rows(
        url,
        "SELECT count(*) FROM fin_thread_attachment_index WHERE attachment_id = %s",
        (attachment_id,),
    )[0][0]
    return int(units), int(postings)


def _attachment_count(url: str, thread_id: str) -> int:
    return int(
        _sql_rows(
            url,
            "SELECT count(*) FROM fin_thread_attachment WHERE thread_id = %s",
            (thread_id,),
        )[0][0]
    )


def _tombstone_row(url: str, attachment_id: str) -> dict[str, Any] | None:
    """The surviving cleanup obligation for one voided attachment, if any."""
    rows = _sql_rows(
        url,
        "SELECT thread_id, object_key FROM fin_thread_attachment_tombstone "
        "WHERE attachment_id = %s",
        (attachment_id,),
    )
    if not rows:
        return None
    return {"thread_id": str(rows[0][0]), "object_key": str(rows[0][1])}


def _tombstone_count(url: str, thread_id: str) -> int:
    """How many byte deletions one thread still owes."""
    return int(
        _sql_rows(
            url,
            "SELECT count(*) FROM fin_thread_attachment_tombstone "
            "WHERE thread_id = %s",
            (thread_id,),
        )[0][0]
    )


def _thread_row(url: str, thread_id: str) -> bool:
    return bool(
        _sql_rows(url, "SELECT 1 FROM fin_thread WHERE id = %s", (thread_id,))
    )


def _fresh_object_read(key: str) -> tuple[bytes | None, str | None]:
    """Read one key through a NEW object-store client in this process.

    Returns ``(payload, outcome)`` where ``outcome`` is ``"present"``,
    ``"absent"`` — the adapter's own missing-key failure — or the name of
    whatever else the store answered. A service that is down and a key that
    is gone are read off the same adapter, and only one of them is evidence
    about a delete, so the caller gets to name which it is treating as
    which instead of collapsing both into "gone".
    """
    from fin.corpus.object_store import create_object_store
    from fin.corpus.object_store_errors import UnknownObjectKeyError

    try:
        return create_object_store().get(key), "present"
    except UnknownObjectKeyError:
        # EXIT: the store answered — the key is simply not in it, which is
        # what a completed cleanup looks like from a second client.
        return None, "absent"
    except Exception as error:  # noqa: BLE001 - the type name IS the result
        return None, type(error).__name__


def _delete_object_now(key: str) -> tuple[bool, str | None]:
    """Remove one live-test object through a fresh client.

    Returns whether it is gone and, when it is not, what the store answered.
    Never raises: this runs in ``finally`` blocks, where an exception would
    replace the failure a test is trying to report.
    """
    from fin.corpus.object_store import create_object_store

    try:
        create_object_store().delete(key)
    except Exception as error:  # noqa: BLE001 - reported to the caller
        return False, type(error).__name__
    _, outcome = _fresh_object_read(key)
    if outcome != "absent":
        return False, outcome if outcome != "present" else "still-present"
    return True, None


def _drop_threads(url: str, thread_ids: list[str]) -> None:
    """Remove every thread a test created; the rows and postings cascade."""
    if not thread_ids:
        return
    import psycopg

    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM fin_thread WHERE id = ANY(%s)", (thread_ids,))
        conn.commit()


def _s3_object_store_configured() -> bool:
    endpoint = (
        os.environ.get("FIN_OBJECT_STORE_URL")
        or os.environ.get("FIN_OBJECT_STORE_ENDPOINT")
        or ""
    ).strip()
    bucket = (os.environ.get("FIN_OBJECT_STORE_BUCKET") or "").strip()
    return bool(endpoint and bucket)


def _cleanup_live_threads(url: str, thread_ids: list[str]) -> None:
    """Delete live-test objects before their rows, even on assertion failure.

    The live path must actually remove its data. ``_drop_threads`` is a
    cascade, so it silently hides an attachment whose object delete failed:
    the row goes and the bytes stay. Deleting every key first, then
    demanding it be gone through a second client, is what makes the
    difference visible. Tombstone rows are swept too, because an
    attachment row that was voided before a failed cleanup is no longer
    there to name its bytes.
    """
    if not thread_ids:
        return

    failures: list[Exception] = []
    rows = _sql_rows(
        url,
        "SELECT object_key FROM fin_thread_attachment "
        "WHERE thread_id = ANY(%s) "
        "UNION SELECT object_key FROM fin_thread_attachment_tombstone "
        "WHERE thread_id = ANY(%s)",
        (thread_ids, thread_ids),
    )
    if rows and _s3_object_store_configured():
        from fin.corpus.object_store import create_object_store
        from fin.corpus.object_store_errors import UnknownObjectKeyError

        objects = create_object_store()
        for (key,) in rows:
            try:
                objects.delete(str(key))
                objects.get(str(key))
            except UnknownObjectKeyError:
                continue
            except Exception as exc:
                failures.append(exc)
            else:
                failures.append(AssertionError("object still exists after delete"))

    if failures:
        error_types = sorted({type(error).__name__ for error in failures})
        raise AssertionError(
            f"failed to delete {len(failures)} live-test object(s); "
            f"error types: {', '.join(error_types)}"
        ) from failures[0]

    _drop_threads(url, thread_ids)


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _payload(path: Path) -> bytes:
    assert path.is_file(), f"committed gold payload missing: {path}"
    return path.read_bytes()


def _sized_to(cap: int, seed: bytes) -> bytes:
    """A deterministic body of exactly ``cap`` bytes, built from real filing bytes."""
    repeats, remainder = divmod(cap, len(seed))
    return seed * repeats + seed[:remainder]


def _unknown_thread_id() -> str:
    """A thread id no row can carry: the door must answer, never create."""
    import uuid

    return f"no-such-thread-{uuid.uuid4().hex}"


# The payloads this acceptance names: every committed gold case plus the
# real filing that straddles the retired constant and the deployed bound.
# A plain table, not ``parametrize``, because both harnesses walk the whole
# set in one pass and assert on each other's storage.
GOLD_CASES: tuple[tuple[str, Path], ...] = (
    (MIXED_PDF.name, MIXED_PDF),
    (SCANNED_PDF.name, SCANNED_PDF),
    (CHART_PNG.name, CHART_PNG),
    (MSFT_Q1_PDF.name, MSFT_Q1_PDF),
)


# =========================================================================
# Harness 1: the real app in this process, so both stores are observable.
# =========================================================================


class _ObjectStoreLens:
    """A pass-through over the product-selected byte store.

    Every call delegates unchanged — the real adapter still does the
    work — and the only thing added is the call history. "The refusal
    left no partial object" is a claim about what the store was ASKED,
    which final contents alone cannot show.
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.puts: list[tuple[str, int]] = []
        self.deletes: list[str] = []

    def put(
        self, key: str, payload: bytes, *, content_type: str | None = None
    ) -> Any:
        self.puts.append((key, len(payload)))
        return self._inner.put(key, payload, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def get_pointer(self, key: str) -> Any:
        return self._inner.get_pointer(key)

    def delete(self, key: str) -> None:
        self.deletes.append(key)
        return self._inner.delete(key)


@pytest.fixture
def door(postgres_url: str, budgets: Any) -> Iterator[dict[str, Any]]:
    """The real ``fin.host.http.app`` on the real Postgres-backed service.

    The service comes from the product's own builder, so the thread
    store, the attachment store, and the byte adapter are the ones a
    ``fin-serve`` on this machine would build. ``_ObjectStoreLens`` adds
    observation without replacing it.
    """
    import fin.host.workspace_http as workspace_http
    from fin.host.attachment_prepare import AttachmentPreparationService
    from fin.host.attachment_store import PostgresAttachmentStore
    from fin.host.http import (
        app,
        get_attachment_budgets,
        get_attachment_service,
        get_graph,
    )

    built = workspace_http._build_default_attachment_service()
    assert isinstance(built.store, PostgresAttachmentStore), (
        "This lane's evidence needs the Postgres attachment store, "
        f"got {type(built.store).__name__}"
    )
    objects = _ObjectStoreLens(built.objects)
    service = AttachmentPreparationService(store=built.store, objects=objects)

    class _NullGraph:
        """The upload door reaches no graph; thread delete reaches one call."""

        def delete_thread(self, thread_id: str) -> None:
            return None

    app.dependency_overrides[get_graph] = lambda: _NullGraph()
    app.dependency_overrides[get_attachment_service] = lambda: service
    app.dependency_overrides[get_attachment_budgets] = lambda: budgets
    try:
        yield {
            "client": TestClient(app),
            "service": service,
            "store": service.store,
            "objects": objects,
            "budgets": budgets,
            "url": postgres_url,
        }
    finally:
        app.dependency_overrides.clear()
        # Every object this test wrote is removed through the same port it
        # was written to, and the removal is checked rather than assumed:
        # a real-storage suite that leaves bytes behind changes what the
        # next run's byte-store assertions see.
        from fin.corpus.object_store_errors import UnknownObjectKeyError

        for key, _ in objects.puts:
            objects.delete(key)
            with pytest.raises(UnknownObjectKeyError):
                objects.get(key)


def _new_thread(client: TestClient) -> str:
    response = client.post("/api/threads", json={})
    assert response.status_code == 200, response.text
    return str(response.json()["id"])


def _upload(client: TestClient, thread_id: str, payload: bytes, filename: str):
    return client.post(
        f"/api/threads/{thread_id}/attachments",
        content=payload,
        params={"filename": filename},
    )


def test_upload_of_each_gold_payload_lands_as_durable_bytes_with_nothing_derived(
    door: dict[str, Any]
) -> None:
    """The door stores raw bytes and stops there, on real storage.

    Per gold payload: a 200 whose ``uploaded`` is also the row the SQL
    table holds; the exact bytes back out of the byte store; and zero
    frozen-unit and zero posting rows for that attachment — the "no
    parse, no OCR, no layout, no vision, no index at upload" clause read
    off the real tables rather than off an in-memory dict.
    """
    client = door["client"]
    service = door["service"]
    store = door["store"]
    url = door["url"]

    for filename, path in GOLD_CASES:
        payload = _payload(path)
        thread_id = _new_thread(client)
        try:
            response = _upload(client, thread_id, payload, filename)
            assert response.status_code == 200, (filename, response.text)
            body = response.json()
            assert body["filename"] == filename, body
            assert body["status"] == "uploaded", body
            assert body["scope"] == "thread_attach", body
            assert body["size_bytes"] == len(payload), body
            assert body["version"] == 0, body
            assert body["unit_count"] == 0, body
            assert body["searchable"] is False, body
            assert body["failure_reason"] is None, body

            attachment_id = str(body["attachment_id"])
            row = _attachment_row(url, attachment_id)
            assert row is not None, attachment_id
            assert row["thread_id"] == thread_id, row
            assert row["status"] == "uploaded", row
            assert int(row["size_bytes"]) == len(payload), row
            assert row["sha256"] == _sha(payload), row
            assert int(row["version"]) == 0 and int(row["unit_count"]) == 0, row
            assert row["indexed"] is False, row
            assert row["failure_reason"] is None, row

            # The bytes really are in the byte store, whole and unchanged.
            assert service.objects.get(str(row["object_key"])) == payload, filename

            # ...and nothing was derived from them.
            assert _derived_counts(url, attachment_id) == (0, 0), row
            record = store.get(attachment_id)
            assert record is not None, attachment_id
            assert store.units(record.attachment_id, record.version) == (), record
            assert store.index_postings(
                record.attachment_id, record.version
            ) == {}, record
            assert store.indexed_versions(record.attachment_id) == (), record
        finally:
            _drop_threads(url, [thread_id])
        assert _attachment_count(url, thread_id) == 0, filename


def test_byte_adapter_is_the_one_the_product_itself_selected(
    door: dict[str, Any]
) -> None:
    """Which raw-byte adapter the deployment resolved, stated as evidence.

    This case exists so the record's limitation is observed rather than
    asserted: the product's builder raises ``ObjectStoreNotConfiguredError``
    when no ``FIN_OBJECT_STORE_URL`` / ``FIN_OBJECT_STORE_BUCKET`` is set
    and takes its documented degradation, which is what ran here. What is
    pinned is that the adapter under test is one of the two implementations
    the product itself uses and not a test double — the durable-S3 leg
    stays unproven on a machine with no object service deployed, and this
    is where that becomes visible.
    """
    from fin.corpus.object_store import (
        InMemoryObjectStore,
        S3CompatibleObjectStore,
    )
    from fin.corpus.object_store_errors import ObjectStoreNotConfiguredError
    from fin.corpus.object_store import create_object_store

    product_adapters = (InMemoryObjectStore, S3CompatibleObjectStore)
    inner = door["service"].objects._inner  # noqa: SLF001 - the claim IS the adapter
    assert isinstance(inner, product_adapters), type(inner).__name__

    configured = bool(
        (os.environ.get("FIN_OBJECT_STORE_URL") or "").strip()
        and (os.environ.get("FIN_OBJECT_STORE_BUCKET") or "").strip()
    )
    if configured:
        assert isinstance(inner, S3CompatibleObjectStore), type(inner).__name__
    else:
        with pytest.raises(ObjectStoreNotConfiguredError):
            create_object_store()
        assert isinstance(inner, InMemoryObjectStore), type(inner).__name__


def test_refusal_at_the_deployed_cap_leaves_no_object_and_no_row(
    door: dict[str, Any]
) -> None:
    """Exactly ``max_upload_bytes`` lands; one byte over is a named 413.

    Both legs of the gate are asked: a declared ``Content-Length`` the
    route refuses unread, and a chunked body with no declared length,
    which only the bounded segment reader can refuse. Both must produce
    the same answer, and neither may leave a partial object behind —
    which is what ``_ObjectStoreLens`` is here to show.
    """
    import fin.host.workspace_http as workspace_http

    budgets = door["budgets"]
    cap = budgets.max_upload_bytes
    assert cap > RETIRED_ROUTE_CAP_BYTES, cap
    # The retired constant is gone from the module, not merely unused.
    assert not hasattr(workspace_http, "ATTACHMENT_MAX_BYTES"), (
        "the 2 MiB route constant must be replaced by the deployed bound"
    )

    client = door["client"]
    objects: _ObjectStoreLens = door["objects"]
    url = door["url"]
    seed = _payload(MIXED_PDF)

    thread_id = _new_thread(client)
    try:
        at_cap = _sized_to(cap, seed)
        assert len(at_cap) == cap
        puts_before = list(objects.puts)
        accepted = _upload(client, thread_id, at_cap, "at-cap.pdf")
        assert accepted.status_code == 200, accepted.text
        accepted_body = accepted.json()
        assert accepted_body["status"] == "uploaded", accepted_body
        assert accepted_body["size_bytes"] == cap, accepted_body
        assert objects.puts == puts_before + [
            (
                f"thread-attachments/{thread_id}/{accepted_body['attachment_id']}",
                cap,
            )
        ], objects.puts

        one_over = _sized_to(cap + 1, seed)
        assert len(one_over) == cap + 1
        refused = _upload(client, thread_id, one_over, "one-over.pdf")
        assert refused.status_code == 413, refused.status_code
        assert refused.json() == {"error": "attachment_too_large"}, refused.text

        # Same size, no declared length: the bounded reader must decide it.
        streamed = client.post(
            f"/api/threads/{thread_id}/attachments",
            content=iter([one_over[: cap // 2], one_over[cap // 2 :]]),
            params={"filename": "streamed-over.pdf"},
        )
        assert streamed.status_code == 413, streamed.status_code
        assert streamed.json() == {"error": "attachment_too_large"}, streamed.text

        # Neither refusal touched the byte store or the attachment table.
        assert objects.puts == puts_before + [
            (
                f"thread-attachments/{thread_id}/{accepted_body['attachment_id']}",
                cap,
            )
        ], objects.puts
        assert _attachment_count(url, thread_id) == 1, _sql_rows(
            url,
            "SELECT attachment_id, filename FROM fin_thread_attachment "
            "WHERE thread_id = %s",
            (thread_id,),
        )
        rows = _sql_rows(
            url,
            "SELECT filename FROM fin_thread_attachment WHERE thread_id = %s",
            (thread_id,),
        )
        assert [row[0] for row in rows] == ["at-cap.pdf"], rows
    finally:
        _drop_threads(url, [thread_id])
    assert _attachment_count(url, thread_id) == 0


def test_upload_starts_no_work_behind_the_response(
    door: dict[str, Any]
) -> None:
    """The response is the whole request: one put, and then nothing happens.

    The superseded door handed the minted row to ``BackgroundTasks``, so a
    second wave of writes landed after the client had already been told.
    This door removed that hand-off; the observable shape is a byte store whose
    call history and a derived-data count that both stay flat after the
    200 — a short settle later, not just in the instant of the answer.
    """
    client = door["client"]
    objects: _ObjectStoreLens = door["objects"]
    url = door["url"]
    payload = _payload(MIXED_PDF)

    thread_id = _new_thread(client)
    try:
        puts_before = list(objects.puts)
        response = _upload(client, thread_id, payload, MIXED_PDF.name)
        assert response.status_code == 200, response.text
        body = response.json()
        attachment_id = str(body["attachment_id"])
        one_put = [
            (f"thread-attachments/{thread_id}/{attachment_id}", len(payload))
        ]
        assert objects.puts == puts_before + one_put, objects.puts

        time.sleep(1.0)
        assert objects.puts == puts_before + one_put, objects.puts
        assert _derived_counts(url, attachment_id) == (0, 0), objects.puts
        row = _attachment_row(url, attachment_id)
        assert row is not None and row["status"] == "uploaded", row
        assert row["failure_reason"] is None, row
    finally:
        _drop_threads(url, [thread_id])


def test_unsupported_bytes_upload_and_only_preparation_names_them(
    door: dict[str, Any]
) -> None:
    """The non-UTF-8 PNG is raw bytes at the door and a rejection at preparation.

    The content judgement lives off the upload path. This is that move
    observed end to end: the 40,419-byte PNG that the superseded door
    answered 400 ``attachment_not_text`` for now answers 200
    ``uploaded`` with no failure reason, and ``attachment_not_text``
    appears only when preparation reads the stored bytes back.
    """
    client = door["client"]
    url = door["url"]
    payload = _payload(CHART_PNG)
    assert len(payload) == 40_419, len(payload)
    with pytest.raises(UnicodeDecodeError):
        payload.decode("utf-8")

    thread_id = _new_thread(client)
    try:
        response = _upload(client, thread_id, payload, CHART_PNG.name)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "uploaded", body
        assert body["failure_reason"] is None, body
        attachment_id = str(body["attachment_id"])
        assert _derived_counts(url, attachment_id) == (0, 0), body

        # T2f: the door queues the durable job and answers before the bytes
        # are read back, so a retried row leaves it as work pending — the
        # naming happens in the drain, on a later status read.
        queued = client.post(
            f"/api/threads/{thread_id}/attachments/{attachment_id}/retry"
        )
        assert queued.status_code == 200, queued.text
        assert queued.json()["status"] == "parsing", queued.json()
        assert door["service"].drain_attachment_jobs() >= 1
        failure = client.get(
            f"/api/threads/{thread_id}/attachments/{attachment_id}"
        ).json()
        assert failure["status"] == "failed", failure
        assert failure["failure_reason"] == "attachment_not_text", failure
        # Naming the content derived nothing either: no units, no postings.
        assert _derived_counts(url, attachment_id) == (0, 0), failure
    finally:
        _drop_threads(url, [thread_id])


def test_empty_body_and_unknown_thread_stay_named_and_store_nothing(
    door: dict[str, Any]
) -> None:
    """Empty input and a thread that does not exist, on the same door."""
    client = door["client"]
    objects: _ObjectStoreLens = door["objects"]
    url = door["url"]
    puts_before = list(objects.puts)

    missing = _upload(client, _unknown_thread_id(), b"", "x.txt")
    assert missing.status_code == 404, missing.text
    assert missing.json() == {"error": "thread_not_found"}, missing.text

    thread_id = _new_thread(client)
    try:
        blank = _upload(client, thread_id, b"", "x.txt")
        assert blank.status_code == 400, blank.text
        assert blank.json() == {"error": "empty_attachment"}, blank.text
        assert _attachment_count(url, thread_id) == 0
    finally:
        _drop_threads(url, [thread_id])
    assert objects.puts == puts_before


def test_concurrent_and_duplicate_uploads_land_as_distinct_rows(
    door: dict[str, Any]
) -> None:
    """The concurrent / duplicate class, on real SQL.

    Two threads racing one gold payload must produce two independent
    rows, neither blocking, merging, or overwriting the other; and a
    re-upload of the same bytes into one thread must be a second row,
    because identity is the attachment, not the content.
    """
    import concurrent.futures

    client = door["client"]
    store = door["store"]
    url = door["url"]
    payload = _payload(SCANNED_PDF)

    threads = [_new_thread(client) for _ in range(2)]
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(
                pool.map(
                    lambda thread_id: _upload(
                        client, thread_id, payload, SCANNED_PDF.name
                    ),
                    threads,
                )
            )
        assert [response.status_code for response in responses] == [200, 200]
        bodies = [response.json() for response in responses]
        ids = [str(body["attachment_id"]) for body in bodies]
        assert len(set(ids)) == 2, ids
        assert all(body["status"] == "uploaded" for body in bodies), bodies
        for attachment_id in ids:
            record = store.get(attachment_id)
            assert record is not None and record.status == "uploaded", record
            assert record.sha256 == _sha(payload), record
            assert _derived_counts(url, attachment_id) == (0, 0), record

        again = _upload(client, threads[0], payload, SCANNED_PDF.name)
        assert again.status_code == 200, again.text
        assert str(again.json()["attachment_id"]) not in ids, again.json()
        listed = client.get(
            f"/api/threads/{threads[0]}/attachments"
        ).json()["attachments"]
        assert len(listed) == 2, listed
        assert {entry["status"] for entry in listed} == {"uploaded"}, listed
        assert _attachment_count(url, threads[0]) == 2
    finally:
        _drop_threads(url, threads)
    assert [
        _attachment_count(url, thread_id) for thread_id in threads
    ] == [0, 0]


def uuid4_hex() -> str:
    import uuid

    return uuid.uuid4().hex


# =========================================================================
# Harness 2: a second real process, so durability is crossed, not asserted.
# =========================================================================

_CHILD_SOURCE = """
import os
import sys

import uvicorn

from fin.host import http as http_module


class _NullGraph:
    def delete_thread(self, thread_id):
        return None


# Only the graph is replaced, and only because a run would need model
# credentials this lane never holds. Everything the door reads — bounds,
# stores, adapters — comes from this process's own environment, so the
# byte cap under test is the one `get_attachment_budgets()` resolved from
# the deployed keys rather than an object this harness handed it.
http_module.app.dependency_overrides[http_module.get_graph] = lambda: _NullGraph()

# One injected fault, at one seam: the deployed attachment service's own
# byte-store client refuses deletes. Everything above it — this process,
# uvicorn, the HTTP door, Postgres, the tombstone record — is the product,
# and the retry leg runs in a SECOND process where the client is whole
# again, so "a failed cleanup retains the tombstone and stays retriable" is
# crossed over a real restart rather than asserted.
if os.environ.get("FIN_LIVE_FAIL_OBJECT_DELETE") == "1":
    def _refuse_delete(key):
        raise RuntimeError("injected object-store delete refusal for " + str(key))

    http_module.default_attachment_service().objects.delete = _refuse_delete

# The product's own execution side, started the way ``serve()`` starts it
# before binding: the preparation doors only queue durable jobs, so a
# process without this drain leaves every queued row at ``parsing`` forever.
http_module.start_attachment_prep_worker(http_module.default_attachment_service())

uvicorn.run(
    http_module.app,
    host="127.0.0.1",
    port=int(sys.argv[1]),
    log_level="warning",
)
"""


_READBACK_CHILD_SOURCE = """
import hashlib
import json
import sys
from pathlib import Path

from fin.corpus.object_store import create_object_store


key, payload_path, expected_sha256 = sys.argv[1:4]
try:
    expected = Path(payload_path).read_bytes()
    actual = create_object_store().get(key)
except Exception as error:
    print(json.dumps({"ok": False, "error_type": type(error).__name__}))
    raise SystemExit(1)

actual_sha256 = hashlib.sha256(actual).hexdigest()
expected_actual_sha256 = hashlib.sha256(expected).hexdigest()
if actual != expected or actual_sha256 != expected_sha256:
    print(json.dumps({"ok": False, "error_type": "PayloadMismatch"}))
    raise SystemExit(1)

print(json.dumps({
    "ok": True,
    "size_bytes": len(actual),
    "sha256": actual_sha256,
    "expected_sha256": expected_actual_sha256,
}))
"""


class _LiveServer:
    """One real uvicorn process serving the real app on a real socket."""

    def __init__(self, port: int, process: subprocess.Popen[str]) -> None:
        self.port = port
        self.process = process

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def request(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        headers = {"content-type": content_type} if content_type else {}
        request = urllib.request.Request(
            f"{self.base}{path}", data=body, method=method, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return response.status, _load(response.read())
        except urllib.error.HTTPError as error:
            return error.code, _load(error.read())

    def create_thread(self) -> str:
        status, payload = self.request(
            "POST", "/api/threads", body=b"{}", content_type="application/json"
        )
        assert status == 200, payload
        assert isinstance(payload, dict), payload
        return str(payload["id"])

    def attach(self, thread_id: str, payload: bytes, filename: str) -> tuple[int, Any]:
        query = urllib.parse.urlencode({"filename": filename})
        return self.request(
            "POST",
            f"/api/threads/{thread_id}/attachments?{query}",
            body=payload,
            content_type="application/octet-stream",
        )

    def prepare(self, thread_id: str, attachment_id: str) -> tuple[int, Any]:
        return self.request(
            "POST",
            f"/api/threads/{thread_id}/attachments/{attachment_id}/retry",
            body=b"",
            content_type="application/json",
        )

    def attachment_status(
        self, thread_id: str, attachment_id: str
    ) -> tuple[int, Any]:
        return self.request(
            "GET", f"/api/threads/{thread_id}/attachments/{attachment_id}"
        )

    def delete_attachment(
        self, thread_id: str, attachment_id: str
    ) -> tuple[int, Any]:
        """The one shipped path, addressed as a client addresses it."""
        from fin.host.workspace_http import _attachment_delete_path

        return self.request(
            "DELETE", _attachment_delete_path(thread_id, attachment_id)
        )

    def delete_thread(self, thread_id: str) -> tuple[int, Any]:
        return self.request("DELETE", f"/api/threads/{thread_id}")

    def stop(self) -> None:
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - child misbehaving
            self.process.kill()
            self.process.wait(timeout=30)


#: The states a preparation job can finish in; anything else is still owed.
_TERMINAL_STATUSES = ("ready", "failed")


def _await_terminal_status(
    live_server: _LiveServer, thread_id: str, attachment_id: str
) -> dict[str, Any]:
    """Read the status door until the child's own drain lands a terminal state.

    The retry door only queues (T2f), so the state this lane is really
    proving arrives from the other process's prep worker. A fixed sleep would
    either race that thread or over-wait it, so the door is polled and the
    failure names what was last seen.
    """
    deadline = time.monotonic() + 120.0
    last: Any = None
    while time.monotonic() < deadline:
        status, last = live_server.attachment_status(thread_id, attachment_id)
        assert status == 200, last
        assert isinstance(last, dict), last
        if last["status"] in _TERMINAL_STATUSES:
            return last
        time.sleep(0.2)
    raise AssertionError(f"attachment never settled: {last!r} (after 120s)")


def _load(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # EXIT: a body that is not JSON — a proxy's plain-text 502, an empty
        # HTML error page — is returned as bytes so the failing assertion
        # reports what actually arrived instead of a decode error that hides
        # it. Every route under test answers JSON.
        return raw


@pytest.fixture(scope="module")
def live_server(
    postgres_url: str, deployed_bounds: dict[str, str]
) -> Iterator[_LiveServer]:
    """Boot the real app in a separate process against the real Postgres."""
    server = _start_live_server(postgres_url, deployed_bounds)
    try:
        yield server
    finally:
        server.stop()


def _start_live_server(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    extra_env: dict[str, str] | None = None,
) -> _LiveServer:
    """Start an independent real Uvicorn child with the deployed bounds."""
    environ = dict(os.environ)
    environ["FIN_CHECKPOINT_URL"] = postgres_url
    environ.update(deployed_bounds)
    environ.update(extra_env or {})

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    process = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SOURCE, str(port)],
        cwd=str(REPO_ROOT),
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    server = _LiveServer(port, process)
    try:
        _wait_for_server(server)
    except Exception:
        server.stop()
        raise
    return server


def _wait_for_server(server: _LiveServer) -> None:
    """Block until the child answers, or fail with the child's own output.

    A child that cannot boot is a FAILED prerequisite, never a skip: this
    module's whole claim is that a real server process served the door.
    """
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.process.poll() is not None:
            tail = server.process.stdout.read() if server.process.stdout else ""
            raise AssertionError(
                f"the live server exited {server.process.returncode}: {tail[-2000:]}"
            )
        try:
            with urllib.request.urlopen(f"{server.base}/docs", timeout=3) as probe:
                if probe.status == 200:
                    return
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.5)
    raise AssertionError(f"the live server never answered on :{server.port}")


def test_live_server_uploads_every_gold_payload_as_uploaded(
    postgres_url: str, live_server: _LiveServer
) -> None:
    """The real process answers ``uploaded``; this process sees the same rows.

    The row is read back over a SECOND connection by a process that never
    handled the request — the part an in-memory store cannot prove.
    """
    seen: list[tuple[str, str, bytes]] = []
    threads: list[str] = []
    try:
        for filename, path in GOLD_CASES:
            payload = _payload(path)
            thread_id = live_server.create_thread()
            threads.append(thread_id)
            status, body = live_server.attach(thread_id, payload, filename)
            assert status == 200, (filename, status, body)
            assert isinstance(body, dict), body
            assert body["status"] == "uploaded", body
            assert body["size_bytes"] == len(payload), body
            assert body["failure_reason"] is None, body
            seen.append((str(body["attachment_id"]), filename, payload))

        for attachment_id, filename, payload in seen:
            row = _attachment_row(postgres_url, attachment_id)
            assert row is not None, filename
            assert row["status"] == "uploaded", row
            assert int(row["size_bytes"]) == len(payload), row
            assert row["sha256"] == _sha(payload), row
            assert int(row["version"]) == 0 and int(row["unit_count"]) == 0, row
            assert _derived_counts(postgres_url, attachment_id) == (0, 0), row
    finally:
        _cleanup_live_threads(postgres_url, threads)


def test_live_upload_bytes_persist_after_uvicorn_exit_in_fresh_adapter_process(
    real_s3_object_store: None,
    postgres_url: str,
    deployed_bounds: dict[str, str],
) -> None:
    """A fresh product adapter reads exact committed bytes after Uvicorn exits.

    The upload is made through the real HTTP app in a real Uvicorn process.
    That process is stopped before another Python process constructs a new
    product adapter and compares the S3 object byte-for-byte with the gold
    payload and its SHA-256.
    """
    payload = _payload(MIXED_PDF)
    expected_sha256 = _sha(payload)
    server: _LiveServer | None = None
    threads: list[str] = []
    try:
        server = _start_live_server(postgres_url, deployed_bounds)
        thread_id = server.create_thread()
        threads.append(thread_id)

        status, body = server.attach(thread_id, payload, MIXED_PDF.name)
        assert status == 200, (status, body)
        assert isinstance(body, dict), body
        assert body["status"] == "uploaded", body
        assert body["size_bytes"] == len(payload), body
        attachment_id = str(body["attachment_id"])

        row = _attachment_row(postgres_url, attachment_id)
        assert row is not None, attachment_id
        assert row["status"] == "uploaded", row
        assert row["sha256"] == expected_sha256, row
        object_key = str(row["object_key"])

        # The next read is made only after the server that performed the
        # upload has fully exited; the new Python process must construct its
        # own adapter from configuration and fetch the same object.
        server.stop()
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                _READBACK_CHILD_SOURCE,
                object_key,
                str(MIXED_PDF),
                expected_sha256,
            ],
            cwd=str(REPO_ROOT),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, (
            "fresh product adapter could not verify the uploaded bytes; "
            f"exit={completed.returncode}, result={completed.stdout.strip()}"
        )
        result = json.loads(completed.stdout)
        assert result == {
            "ok": True,
            "size_bytes": len(payload),
            "sha256": expected_sha256,
            "expected_sha256": expected_sha256,
        }, result
    finally:
        if server is not None:
            server.stop()
        _cleanup_live_threads(postgres_url, threads)


def test_live_server_accepts_the_filing_the_retired_constant_rejected(
    postgres_url: str, live_server: _LiveServer
) -> None:
    """``2023_Q1_MSFT.pdf`` is over the retired cap and under the deployed one.

    This is the case that makes the byte cap a deployment fact rather
    than a route detail: ``2,223,272 > 2,097,152``, so the retired route
    constant answered 413 for a real filing the shipped door now accepts.
    """
    payload = _payload(MSFT_Q1_PDF)
    assert len(payload) == 2_223_272, len(payload)
    assert RETIRED_ROUTE_CAP_BYTES < len(payload), (
        "this case is only evidence while the retired cap sits below the file"
    )

    thread_id = live_server.create_thread()
    try:
        status, body = live_server.attach(thread_id, payload, MSFT_Q1_PDF.name)
        assert status == 200, (status, body)
        assert isinstance(body, dict) and body["status"] == "uploaded", body
        row = _attachment_row(postgres_url, str(body["attachment_id"]))
        assert row is not None and row["status"] == "uploaded", row
        assert int(row["size_bytes"]) == len(payload), row
    finally:
        _cleanup_live_threads(postgres_url, [thread_id])


def test_live_server_uploaded_row_is_not_a_recovery_candidate(
    postgres_url: str, live_server: _LiveServer
) -> None:
    """A restart leaves an ``uploaded`` row alone; it still resumes ``parsing``.

    ``incomplete()`` is the recovery selector, and a restart of the real
    service runs it at construction. The control at the end is what makes
    the clean answer above evidence rather than a query that never
    matches: the same selector, against the same tables, DOES report a
    row whose status is ``parsing``.
    """
    import psycopg

    from fin.host.attachment_store import PostgresAttachmentStore

    payload = _payload(MIXED_PDF)
    uploaded_thread = live_server.create_thread()
    status, body = live_server.attach(uploaded_thread, payload, MIXED_PDF.name)
    assert status == 200, (status, body)
    assert isinstance(body, dict) and body["status"] == "uploaded", body
    uploaded_id = str(body["attachment_id"])

    control_thread = live_server.create_thread()
    control_status, control_body = live_server.attach(
        control_thread, payload, MIXED_PDF.name
    )
    assert control_status == 200, control_status
    assert isinstance(control_body, dict)
    control_id = str(control_body["attachment_id"])

    try:
        # A second store instance is a new process's view of the same SQL.
        restarted = PostgresAttachmentStore(postgres_url)
        listed = restarted.list_for_thread(uploaded_thread)
        assert [record.attachment_id for record in listed] == [uploaded_id], listed
        assert listed[0].status == "uploaded", listed[0]

        recovered = restarted.incomplete()
        recovered_ids = {record.attachment_id for record in recovered}
        assert uploaded_id not in recovered_ids, recovered_ids
        assert all(
            record.status != "uploaded" for record in recovered
        ), "an uploaded row must never be a recovery candidate"
        assert _derived_counts(postgres_url, uploaded_id) == (0, 0)

        # Control: flip one row to ``parsing`` the way an interrupted
        # preparation would, and the same selector must now name it.
        with psycopg.connect(postgres_url) as conn:
            conn.execute(
                "UPDATE fin_thread_attachment SET status = 'parsing' "
                "WHERE attachment_id = %s",
                (control_id,),
            )
            conn.commit()

        again = PostgresAttachmentStore(postgres_url)
        second_ids = {record.attachment_id for record in again.incomplete()}
        assert control_id in second_ids, second_ids
        assert uploaded_id not in second_ids, second_ids
    finally:
        _cleanup_live_threads(postgres_url, [uploaded_thread, control_thread])


def test_live_single_attachment_delete_voids_the_real_bytes(
    real_s3_object_store: None,
    postgres_url: str, live_server: _LiveServer
) -> None:
    """A completed deletion is confirmed at the real store, not in a fake.

    Everything on this path is deployed: real Uvicorn, real ``fetch``-shaped
    HTTP, the object-store adapter
    :func:`fin.corpus.object_store.create_object_store` selects from the
    environment, and the tombstone table on the live Postgres. The read-back
    goes through a SECOND store client this process never used for the
    request, so "the bytes are gone" is the store's answer rather than the
    in-memory stand-in's bookkeeping.
    """
    from fin.corpus.object_store import create_object_store

    payload = b"Q3 revenue rose eleven percent on renewals\n"
    thread_id = live_server.create_thread()
    object_key = ""
    try:
        status, uploaded = live_server.attach(thread_id, payload, "voided.txt")
        assert status == 200, (status, uploaded)
        assert isinstance(uploaded, dict) and uploaded["status"] == "uploaded"
        attachment_id = str(uploaded["attachment_id"])
        row = _attachment_row(postgres_url, attachment_id)
        assert row is not None, attachment_id
        object_key = str(row["object_key"])
        # The bytes are in the real bucket before anything deletes them.
        assert create_object_store().get(object_key) == payload

        status, queued = live_server.prepare(thread_id, attachment_id)
        assert status == 200, (status, queued)
        # The door promises queued work and nothing more; publishing the
        # bytes is the child process's own prep drain, across the socket.
        assert isinstance(queued, dict) and queued["status"] == "parsing", queued
        prepared = _await_terminal_status(live_server, thread_id, attachment_id)
        assert prepared["status"] == "ready", prepared
        assert _derived_counts(postgres_url, attachment_id)[0] > 0

        status, voided = live_server.delete_attachment(thread_id, attachment_id)
        assert status == 200, (status, voided)
        assert voided == {"ok": True, "attachment_id": attachment_id}

        # A second client, in a process that never served the request, is
        # the one that says the raw bytes are gone.
        assert _fresh_object_read(object_key) == (None, "absent")
        assert _attachment_row(postgres_url, attachment_id) is None
        assert _derived_counts(postgres_url, attachment_id) == (0, 0)
        assert _tombstone_row(postgres_url, attachment_id) is None
        assert _tombstone_count(postgres_url, thread_id) == 0

        # The void holds for the doors that would have published or read it.
        assert live_server.attachment_status(thread_id, attachment_id)[1] == {
            "error": "attachment_not_found"
        }
        assert live_server.prepare(thread_id, attachment_id)[1] == {
            "error": "attachment_not_found"
        }
        assert _derived_counts(postgres_url, attachment_id) == (0, 0)

        # A retry of a finished deletion cannot report the bytes as still owed.
        assert live_server.delete_attachment(thread_id, attachment_id) == (
            400,
            {"error": "unknown_locator"},
        )
        object_key = ""
    finally:
        if object_key:
            removed, why = _delete_object_now(object_key)
            assert removed, why
        _cleanup_live_threads(postgres_url, [thread_id])


def test_live_cleanup_failure_keeps_the_tombstone_across_a_real_restart(
    postgres_url: str,
    deployed_bounds: dict[str, str],
    real_s3_object_store: None,
) -> None:
    """A refused byte delete stays observable, blocked, and retryable.

    The fault lives in the first server process: the deployed attachment
    service's own byte client refuses deletes. That process is then stopped
    and a SECOND process, with intact wiring, is what finishes the job — so
    the retained tombstone and its outstanding obligation are carried by the
    real tables and the real bucket, not by an object in a test process.
    """
    payload = b"Q4 margin held because support headcount did not grow\n"
    blocking = _start_live_server(
        postgres_url, deployed_bounds, {"FIN_LIVE_FAIL_OBJECT_DELETE": "1"}
    )
    thread_id = ""
    attachment_id = ""
    object_key = ""
    try:
        thread_id = blocking.create_thread()
        status, uploaded = blocking.attach(thread_id, payload, "owed.txt")
        assert status == 200, (status, uploaded)
        assert isinstance(uploaded, dict)
        attachment_id = str(uploaded["attachment_id"])
        row = _attachment_row(postgres_url, attachment_id)
        assert row is not None, attachment_id
        object_key = str(row["object_key"])

        status, failed = blocking.delete_attachment(thread_id, attachment_id)
        assert status == 500, (status, failed)
        assert failed == {"error": "attachment_cleanup_failed"}

        # Access is voided even though the bytes are still there: the row is
        # gone, its derived layers are gone, and the obligation is named.
        assert _attachment_row(postgres_url, attachment_id) is None
        assert _derived_counts(postgres_url, attachment_id) == (0, 0)
        obligation = _tombstone_row(postgres_url, attachment_id)
        assert obligation is not None
        assert obligation["thread_id"] == thread_id
        assert obligation["object_key"] == object_key
        assert _fresh_object_read(object_key)[1] == "present"

        # A different request, same broken process, still cannot publish.
        assert blocking.prepare(thread_id, attachment_id)[1] == {
            "error": "attachment_not_found"
        }
        assert _tombstone_count(postgres_url, thread_id) == 1
    finally:
        blocking.stop()

    if not object_key:
        pytest.fail("the refused cleanup named no object key to retry against")

    # The restart: a fresh process with the deployed wiring and a byte client
    # that works again, deleting the same identity through the same door.
    restarted = _start_live_server(postgres_url, deployed_bounds)
    thread_done = ""
    try:
        assert restarted.delete_attachment(thread_id, attachment_id) == (
            200,
            {"ok": True, "attachment_id": attachment_id},
        )
        assert _fresh_object_read(object_key) == (None, "absent")
        assert _tombstone_row(postgres_url, attachment_id) is None
        assert _tombstone_count(postgres_url, thread_id) == 0
        object_key = ""

        # Thread deletion afterwards keeps the same rule with nothing owed.
        status, body = restarted.delete_thread(thread_id)
        assert status == 200, (status, body)
        assert _thread_row(postgres_url, thread_id) is False
        thread_done = thread_id
        thread_id = ""
    finally:
        restarted.stop()
        if object_key:
            removed, why = _delete_object_now(object_key)
            assert removed, why
        _cleanup_live_threads(postgres_url, [thread_id or thread_done])
    assert _tombstone_count(postgres_url, thread_done) == 0


def test_module_leaves_the_real_tables_as_it_found_them(
    postgres_url: str
) -> None:
    """Re-runnability.

    A real-storage suite that leaves rows behind changes what the next
    run's ``incomplete()`` sees, which is exactly the assertion the
    recovery case depends on. Every thread this module created is gone, so
    every attachment row, frozen unit, posting, and byte-cleanup obligation
    it owned is gone with the cascade.
    """
    rows = _sql_rows(
        postgres_url,
        "SELECT count(*) FROM fin_thread_attachment WHERE filename IN "
        "('g4-mixed-chart-001.pdf', 'g4-scanned-001.pdf', "
        "'g4-image-chart-001.png', '2023_Q1_MSFT.pdf', 'at-cap.pdf', "
        "'one-over.pdf', 'streamed-over.pdf', 'x.txt', 'voided.txt', "
        "'owed.txt')",
    )[0][0]
    assert int(rows) == 0, rows
    orphans = _sql_rows(
        postgres_url,
        "SELECT count(*) FROM fin_thread_attachment a WHERE NOT EXISTS "
        "(SELECT 1 FROM fin_thread t WHERE t.id = a.thread_id)",
    )[0][0]
    assert int(orphans) == 0, orphans
    derived_orphans = _sql_rows(
        postgres_url,
        "SELECT (SELECT count(*) FROM fin_thread_attachment_unit u WHERE NOT "
        "EXISTS (SELECT 1 FROM fin_thread_attachment a "
        "WHERE a.attachment_id = u.attachment_id)) + "
        "(SELECT count(*) FROM fin_thread_attachment_index i WHERE NOT "
        "EXISTS (SELECT 1 FROM fin_thread_attachment a "
        "WHERE a.attachment_id = i.attachment_id))",
    )[0][0]
    assert int(derived_orphans) == 0, derived_orphans
    # The deletion cases keep their raw bytes until the door confirms the
    # cleanup, so a stranded obligation is exactly what this guard is for:
    # no thread survived, so no tombstone row and no owed object may either.
    obligations = _sql_rows(
        postgres_url, "SELECT count(*) FROM fin_thread_attachment_tombstone"
    )[0][0]
    assert int(obligations) == 0, obligations

