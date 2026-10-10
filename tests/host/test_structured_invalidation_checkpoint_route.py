"""``GET /api/structured-evidence/invalidation-checkpoint``.

The drain worker writes the invalidation checkpoint from inside its own
process on a cadence, so before this surface the only way to learn a
consumer's position was to BE the drain: a stalled drain and a drain
that never started looked identical from outside. The route reads the
store seam, so it is the one place a poller can watch
``acked_checkpoint`` / ``applied_checkpoint`` separately (ADR 0097 §2.4
— they are two facts, and the gap between them is the unacknowledged
window a crash redelivers from).

Two properties are load-bearing rather than cosmetic and are pinned
here: a blank consumer id is a client error instead of a silent
normalisation to the empty consumer, and a store outage is the same
typed 503 ``GET /api/threads/{thread_id}`` answers with — never the
empty position, which would read as "never drained" for a deployment
that has been draining for days.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from fin.host.thread_store import (
    EvidenceInvalidationCheckpoint,
    InMemoryThreadStore,
    PersistenceUnavailableError,
    ThreadStore,
)


ROUTE = "/api/structured-evidence/invalidation-checkpoint"


class _UnreachableThreadStore:
    """A store whose checkpoint read is a storage outage.

    ``PersistenceUnavailableError`` is the typed outcome
    ``PostgresThreadStore._guarded`` raises when the database cannot be
    reached, so this double reproduces the durable path's failure rather
    than inventing a new one.
    """

    def read_invalidation_checkpoint(
        self, consumer_id: str
    ) -> EvidenceInvalidationCheckpoint:
        raise PersistenceUnavailableError("no route to the checkpoint table")


def _client(store: ThreadStore) -> TestClient:
    from fin.host.workspace_http import build_router

    app = FastAPI()
    app.include_router(
        build_router(get_graph=lambda: None, get_thread_store=lambda: store)
    )
    return TestClient(app)


@pytest.fixture
def store() -> ThreadStore:
    return InMemoryThreadStore()


@pytest.fixture
def client(store: ThreadStore) -> TestClient:
    return _client(store)


# ----- the route -----


def test_route_returns_all_four_checkpoint_fields(
    client: TestClient, store: ThreadStore
) -> None:
    """The empty position is a rendered answer, not an absent one: a
    poller must be able to tell "never drained" from "no route"."""
    response = client.get(ROUTE, params={"consumer_id": "fin-default"})

    assert response.status_code == 200
    assert response.json() == {
        "consumer_id": "fin-default",
        "acked_checkpoint": "",
        "applied_checkpoint": "",
        "last_seq": 0,
    }


def test_route_reports_a_scrubbed_and_acknowledged_consumer(
    client: TestClient, store: ThreadStore
) -> None:
    """Driven through the store's own two writes, so the route is
    asserted against the state the drain actually produces rather than
    a hand-built dataclass."""
    store.scrub_structured_evidence_payloads(
        consumer_id="fin-default",
        evidence_refs=[],
        next_checkpoint="applied-token",
        last_seq=7,
    )
    store.advance_invalidation_acknowledgement("fin-default", "acked-token")

    body = client.get(ROUTE, params={"consumer_id": "fin-default"}).json()

    assert body == {
        "consumer_id": "fin-default",
        "acked_checkpoint": "acked-token",
        "applied_checkpoint": "applied-token",
        "last_seq": 7,
    }


def test_route_keeps_acked_and_applied_apart(client: TestClient, store: ThreadStore) -> None:
    """A scrub that committed without an acknowledged cursor is the
    state the two-phase machine exists for; collapsing the fields would
    report it as finished."""
    store.scrub_structured_evidence_payloads(
        consumer_id="fin-default",
        evidence_refs=[],
        next_checkpoint="applied-token",
        last_seq=4,
    )

    body = client.get(ROUTE, params={"consumer_id": "fin-default"}).json()

    assert body["applied_checkpoint"] == "applied-token"
    assert body["acked_checkpoint"] == ""


def test_route_answers_per_consumer(client: TestClient, store: ThreadStore) -> None:
    """The watermark is per ``consumer_id`` (ADR 0097 §2.4 makes
    ``consumer_id`` the primary key), so one consumer's progress must
    never be reported for another's."""
    store.scrub_structured_evidence_payloads(
        consumer_id="fin-default",
        evidence_refs=[],
        next_checkpoint="default-applied",
        last_seq=3,
    )
    store.advance_invalidation_acknowledgement("other-consumer", "other-acked")

    default_body = client.get(ROUTE, params={"consumer_id": "fin-default"}).json()
    other_body = client.get(ROUTE, params={"consumer_id": "other-consumer"}).json()

    assert default_body == {
        "consumer_id": "fin-default",
        "acked_checkpoint": "",
        "applied_checkpoint": "default-applied",
        "last_seq": 3,
    }
    assert other_body == {
        "consumer_id": "other-consumer",
        "acked_checkpoint": "other-acked",
        "applied_checkpoint": "",
        "last_seq": 0,
    }


# ----- boundary classes -----


@pytest.mark.parametrize("consumer_id", ["", " ", "\t", "\n  \t"])
def test_route_rejects_a_blank_consumer_id(client: TestClient, consumer_id: str) -> None:
    """Whitespace-only is an operator's typo, not the empty consumer:
    trimming it to empty and answering would produce a real-looking
    200 for an identity that was never named."""
    response = client.get(ROUTE, params={"consumer_id": consumer_id})

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_consumer_id"}


def test_route_trims_a_padded_consumer_id_before_reading(
    client: TestClient, store: ThreadStore
) -> None:
    """Trimming is normalisation of the NAMED identity, not a fallback:
    the store is asked about the trimmed id and the answer names it."""
    store.scrub_structured_evidence_payloads(
        consumer_id="fin-default",
        evidence_refs=[],
        next_checkpoint="applied-token",
        last_seq=2,
    )

    response = client.get(ROUTE, params={"consumer_id": "  fin-default  "})

    assert response.status_code == 200
    assert response.json()["consumer_id"] == "fin-default"
    assert response.json()["applied_checkpoint"] == "applied-token"


def test_route_requires_the_consumer_id(client: TestClient) -> None:
    """Required, not defaulted to ``INVALIDATION_DRAIN_CONSUMER_ID``: a
    route-level copy of that contract would silently answer for a
    consumer nothing drains, and report its zero as a real position."""
    response = client.get(ROUTE)

    assert response.status_code == 422


def test_route_surfaces_a_store_outage_as_typed_503() -> None:
    """Same posture as ``GET /api/threads/{thread_id}``: a typed 503 body,
    never a bare 500 and never the empty position."""
    response = _client(_UnreachableThreadStore()).get(  # type: ignore[arg-type]
        ROUTE, params={"consumer_id": "fin-default"}
    )

    assert response.status_code == 503
    assert response.json() == {"error": "persistence_unavailable"}

class _RecordingThreadStore:
    """Delegates to an in-memory store and records every method reached.

    The acceptance clause is a READ-ONLY surface, and the in-memory cases
    alone cannot show that: their doubles happen to implement the scrub and
    acknowledgement methods, so a handler that called one would still pass.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self._inner = InMemoryThreadStore()  # type: ignore[call-arg]

    def __getattr__(self, name: str) -> object:
        self.calls.append(name)
        return getattr(self._inner, name)


def test_get_reaches_only_the_checkpoint_read() -> None:
    store = _RecordingThreadStore()
    response = _client(store).get(  # type: ignore[arg-type]
        ROUTE, params={"consumer_id": "fin-default"}
    )

    assert response.status_code == 200
    assert set(store.calls) == {"read_invalidation_checkpoint"}


def test_openapi_publishes_the_response_schema() -> None:
    """Without ``response_model`` the route would keep working while its
    published contract silently changed, so the schema is part of what this
    route promises."""
    schema = _client(InMemoryThreadStore()).get("/openapi.json").json()  # type: ignore[call-arg]
    ref = schema["paths"]["/api/structured-evidence/invalidation-checkpoint"]["get"][
        "responses"
    ]["200"]["content"]["application/json"]["schema"]["$ref"]

    name = ref.rsplit("/", 1)[-1]
    assert name == "InvalidationCheckpointResponse"
    fields = set(schema["components"]["schemas"][name]["properties"])
    assert fields == {
        "consumer_id",
        "acked_checkpoint",
        "applied_checkpoint",
        "last_seq",
    }
