"""The corpus identity mapping: one document under more than one identifier.

The corpus accepts the same bytes under two identifier forms — the generic
leg mints a corpus-owned id, the financial leg mints a ``{TICKER}-{FYQ}``
one — and the two names are ONE document. The mapping says which id the
second name resolves to. It adds resolution ONLY: it never renames,
re-derives or drops an identifier, a locator, a source hash, a scope or a
tombstone, and the frozen identity FORMULAS stay exactly as they were.

Two read points have to know that, and they are here too: the unit-text
lookup behind ``read_source`` and the access-scope filter that narrows a
company's hits to the documents the operator selected. Without the
resolution, an alias id silently narrows the scope to nothing — the
selection names the document, the hit carries the other identifier, and
every hit that belongs to it is dropped.

The accept-time cases run against a real PostgreSQL database, the catalog
cases against a real catalog file under a temporary root, and the scope case
through the REAL shipped graph (``compile_graph``, ``InMemorySaver``) with a
committed selection on the thread's own state channel, so what is asserted
is the host's own narrowing and not a re-implementation of it. The Postgres
cases are skipped when no database is named, and they run in a PRIVATE
``_test`` database this session creates and drops: the suite empties the
mapping's own tables, which is only safe in a database no one else uses.

This file holds the retrieve-side cases as well: ``tests/retrieve`` and
``tests/corpus`` share no package marker, so two files of this name would
collide under pytest's import mode and break collection for both.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from fin.corpus.catalog import list_catalog_entries, list_filings, record_filings
from fin.corpus.document_identity import (
    CORPUS_DOCUMENT_ID_PREFIX,
    DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS,
    DocumentIdentityConfigError,
    DocumentIdentityConnectBoundError,
    DocumentIdentityError,
    DocumentIdentityMapping,
    DocumentIdentityStore,
    DocumentIdentityStoreUnavailableError,
    InMemoryDocumentIdentityStore,
    PostgresDocumentIdentityStore,
    canonical_document_id,
    canonical_document_ids,
    default_document_identity_store,
    document_identity_store_from_env,
    identity_connect_timeout_seconds,
    reset_default_document_identity_store,
    set_default_document_identity_store,
)
from fin.corpus.extracted import ExtractedPage, Locator
from fin.corpus.ingest_endpoint import (
    IngestDocumentIdMissingError,
    IngestDurableCommitError,
    IngestEndpoint,
    split_company_period_from_document_id,
)
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import (
    InMemoryMetadataStore,
    MetadataPointerNotFoundError,
)
from fin.corpus.object_store import InMemoryObjectStore, UnknownObjectKeyError
from fin.host import ScriptedChatModel, compile_graph
from fin.host.source_scope import SourceScopeSnapshot
from fin.host.state import reset_input_state
from fin.retrieve import Chunk
from fin.retrieve.fake_port import FakeRetrievePort as _FakeRetrievePort
from fin.retrieve.fake_push_port import FakePushPort
from fin.retrieve.read_port import unit_text_from_pages, validate_company_source_ref
from fin.retrieve.source_read import INVALID_LOCATOR

TEST_DSN_ENV = "FIN_TEST_DSN"
IDENTITY_DSN_ENV = "FIN_DOCUMENT_IDENTITY_DSN"
CHECKPOINT_DSN_ENV = "FIN_CHECKPOINT_URL"
IDENTITY_CONNECT_TIMEOUT_ENV = "FIN_DOCUMENT_IDENTITY_CONNECT_TIMEOUT_SECONDS"

#: A database nobody listens on: the connection is refused at the TCP
#: level, so the case exercises an unreachable identity database without
#: naming a credential or a real host.
UNREACHABLE_IDENTITY_DSN = "postgresql://fin@127.0.0.1:1/nope"

_SCOPE = "fin-default"
_BYTES = (
    "# Team handbook\n\n"
    "Onboarding starts with a laptop and an account.\n"
).encode("utf-8")
_OTHER_BYTES = (
    "# Expense policy\n\n"
    "Claim receipts within thirty days.\n"
).encode("utf-8")
_VERSION = hashlib.sha256(_BYTES).hexdigest()

# The scope cases: one corpus document, two identifier forms.
GROUNDED = "What was AAPL net sales in 2023 Q3?"
THREAD_ID = "thread-alias-scope"
CANONICAL_ID = "AAPL-2023Q3"
ALIAS_ID = "doc_alias_of_aapl_2023q3"
CORPUS_VERSION = "v" * 8
DOC_TEXT = "Apple net sales for the fiscal 2023 third quarter were 89,498 million."


def _database_name(dsn: str) -> str:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT current_database()").fetchone()
    return str(row[0]) if row is not None else ""


@pytest.fixture(scope="session")
def identity_database() -> Iterator[str]:
    """A private ``_test`` database that holds this session's mapping.

    The DSN comes from ``FIN_TEST_DSN`` and from nothing else: another
    subsystem's DSN names the operator's own database, and this suite
    empties the mapping's tables. A database whose name does not end with
    ``_test`` is REFUSED, not skipped, because connecting to a development
    database would destroy the operator's data.

    The mapping then lives in a private database this session creates and
    drops, so the whole-table empty below can never touch a row that
    belongs to someone else.
    """
    dsn = os.environ.get(TEST_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(
            f"{TEST_DSN_ENV} is unset; the durable document identity mapping "
            "is untested in this environment"
        )
    database = _database_name(dsn)
    assert database.endswith("_test"), (
        f"the identity tests refuse a database that is not a _test database: "
        f"{database}"
    )
    info = conninfo_to_dict(dsn)
    name = f"fin_identity_{uuid.uuid4().hex[:10]}_test"
    assert name.endswith("_test"), "the private database must be a _test database"
    admin = make_conninfo(**{**info, "dbname": "postgres"})
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    try:
        with psycopg.connect(make_conninfo(**{**info, "dbname": name})) as conn:
            assert conn.execute("SELECT current_database()").fetchone()[0] == name
        yield make_conninfo(**{**info, "dbname": name})
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(
                sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                    sql.Identifier(name)
                )
            )


@pytest.fixture
def identity_store(identity_database: str) -> Iterator[PostgresDocumentIdentityStore]:
    """A real identity store on empty tables, before and after the case.

    The empty is safe here: the tables live in the private database this
    session owns, so no other run's rows can be removed.
    """
    store = PostgresDocumentIdentityStore(identity_database)
    _wipe(identity_database)
    yield store
    _wipe(identity_database)
    store.close()


@pytest.fixture(autouse=True)
def _default_store_is_ours() -> Iterator[None]:
    """The process-wide identity store is this suite's to hand.

    The read points resolve through the process default, so a case that
    means "no mapping" has to say so explicitly and every case has to put
    the process back the way it found it.
    """
    set_default_document_identity_store(None)
    yield
    reset_default_document_identity_store()


def _wipe(url: str) -> None:
    with psycopg.connect(url) as conn:
        conn.execute("DELETE FROM fin_corpus_document_alias")
        conn.execute("DELETE FROM fin_corpus_document_content")
        conn.commit()


def _endpoint(
    *,
    identity_store: DocumentIdentityStore | None = None,
    **overrides: Any,
) -> IngestEndpoint:
    kwargs: dict[str, Any] = {
        "push_port": FakePushPort(),
        "object_store": InMemoryObjectStore(),
        "metadata_store": InMemoryMetadataStore(),
        "state_store": InMemoryIngestStateStore(),
        "corpus_scope_id": _SCOPE,
    }
    if identity_store is not None:
        kwargs["identity_store"] = identity_store
    kwargs.update(overrides)
    return IngestEndpoint(**kwargs)


def _expected_generic_id(data: bytes) -> str:
    """The generic id, spelled out here rather than borrowed.

    The formula is FROZEN: ``doc_`` + sha256(domain‖scope‖NUL‖sha256(bytes)).
    Computing it here proves the accept still mints that id, rather than
    proving that the accept agrees with itself.
    """
    digest = hashlib.sha256(data).digest()
    preimage = (
        b"fin-corpus-document-v1\x00"
        + _SCOPE.encode("utf-8")
        + b"\x00"
        + digest
    )
    return "doc_" + hashlib.sha256(preimage).hexdigest()


def _alias_mapping(store: Any) -> None:
    """Record the alias the two identifier forms stand for."""
    store.record_document(document_id=CANONICAL_ID, version=CORPUS_VERSION)
    store.record_document(document_id=ALIAS_ID, version=CORPUS_VERSION)


# ---------- accept: the alias is recorded ----------


def test_the_same_bytes_accepted_twice_record_the_second_id_as_an_alias(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The same bytes under two identifier forms are ONE document.

    The public file-only leg mints the corpus-owned id, the trusted
    explicit-ID seam names the ``{TICKER}-{FYQ}`` one, and the second is
    recorded as an ALIAS of the first. The accept itself is untouched: each
    upload is still accepted under the id its own leg produced.
    """
    endpoint = _endpoint(identity_store=identity_store)

    generic = endpoint.submit_durable(_BYTES, filename="team handbook.md")
    financial = endpoint.submit_durable(
        _BYTES,
        document_id="AAPL-2024Q3",
        company="AAPL",
        period="2024Q3",
        declared_format="md",
    )

    assert generic.document_id == _expected_generic_id(_BYTES)
    assert financial.document_id == "AAPL-2024Q3"
    resolved = identity_store.canonical_document_ids(
        [generic.document_id, financial.document_id]
    )
    assert resolved[financial.document_id] == generic.document_id


def test_the_first_identifier_stays_canonical(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """First writer wins, and the canonical id is never renamed.

    The first id keeps resolving to ITSELF: the mapping adds a second name
    for the document, it does not move the document.
    """
    endpoint = _endpoint(identity_store=identity_store)

    generic = endpoint.submit_durable(_BYTES, filename="team handbook.md")
    endpoint.submit_durable(
        _BYTES,
        document_id="AAPL-2024Q3",
        company="AAPL",
        period="2024Q3",
        declared_format="md",
    )

    resolved = identity_store.canonical_document_ids([generic.document_id])
    assert resolved[generic.document_id] == generic.document_id


def test_different_bytes_are_not_an_alias(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """Only the SAME content makes a second identifier an alias.

    Two documents with different content are two documents, however similar
    their identifier forms look.
    """
    endpoint = _endpoint(identity_store=identity_store)

    generic = endpoint.submit_durable(_BYTES, filename="team handbook.md")
    endpoint.submit_durable(
        _OTHER_BYTES,
        document_id="AAPL-2024Q3",
        company="AAPL",
        period="2024Q3",
        declared_format="md",
    )

    resolved = identity_store.canonical_document_ids(["AAPL-2024Q3"])
    assert resolved["AAPL-2024Q3"] == "AAPL-2024Q3"
    assert generic.document_id != "AAPL-2024Q3"


def test_an_accept_without_an_identity_store_records_nothing() -> None:
    """No database named, no mapping: the corpus behaves as it did before.

    The mapping is additive infrastructure, so a deployment that named no
    identity store must keep accepting uploads exactly as it did.
    """
    endpoint = _endpoint()

    generic = endpoint.submit_durable(_BYTES, filename="team handbook.md")
    financial = endpoint.submit_durable(
        _BYTES,
        document_id="AAPL-2024Q3",
        company="AAPL",
        period="2024Q3",
        declared_format="md",
    )

    assert generic.document_id == _expected_generic_id(_BYTES)
    assert financial.document_id == "AAPL-2024Q3"


# ---------- the mapping is monotonic ----------


def test_a_content_version_keeps_its_first_owner_across_a_content_replacement(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """A replaced document keeps its claim, so the bytes keep ONE owner.

    A document that takes new bytes does not give up the bytes it held
    first: the claim on those bytes stays with the document that first
    held them. A later document presenting those bytes therefore resolves
    to that first owner instead of becoming a second canonical id for the
    same content — the mapping stays monotonic.
    """
    first = identity_store.record_document(document_id="A", version="v1")
    alias = identity_store.record_document(document_id="B", version="v1")
    replaced = identity_store.record_document(document_id="A", version="v2")
    later = identity_store.record_document(document_id="C", version="v1")

    assert first.canonical_document_id == "A"
    assert alias.canonical_document_id == "A"
    assert replaced.canonical_document_id == "A"
    assert later.canonical_document_id == "A"

    resolved = identity_store.canonical_document_ids(["A", "B", "C"])
    assert resolved["B"] == resolved["C"] == "A"
    assert resolved["A"] == "A"


def test_the_content_claim_of_a_replaced_document_is_not_released(
    identity_database: str,
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The claim rows of a replaced document both stay.

    The claim is what makes the mapping monotonic: releasing it would let
    the same bytes be claimed twice, and the second claim would give the
    same content two canonical ids.
    """
    identity_store.record_document(document_id="A", version="v1")
    identity_store.record_document(document_id="A", version="v2")

    with psycopg.connect(identity_database) as conn:
        rows = conn.execute(
            "SELECT version, document_id FROM fin_corpus_document_content "
            "ORDER BY version"
        ).fetchall()

    assert [tuple(row) for row in rows] == [("v1", "A"), ("v2", "A")]


def test_the_in_memory_store_keeps_the_same_monotonic_contract() -> None:
    """Both stores keep one contract: the same sequence, the same answer.

    The in-process store is the same mapping as the durable one, so a
    caller cannot tell which backend it was handed.
    """
    store = InMemoryDocumentIdentityStore()

    store.record_document(document_id="A", version="v1")
    store.record_document(document_id="B", version="v1")
    store.record_document(document_id="A", version="v2")
    store.record_document(document_id="C", version="v1")

    resolved = store.canonical_document_ids(["A", "B", "C"])
    assert resolved["B"] == resolved["C"] == "A"
    assert resolved["A"] == "A"


# ---------- one content version, one canonical id ----------


def test_bytes_a_second_name_took_stay_owned_by_the_document(
    identity_database: str,
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The claim names the id that HOLDS the document, not the name presented.

    A owns the bytes and B is A's second name for them. When B presents new
    bytes, the claim belongs to A — the id that holds the document — so
    those bytes stay owned. A later id presenting the same bytes then
    resolves to A instead of claiming them and becoming a second owner of
    one content version.
    """
    identity_store.record_document(document_id="A", version="v1")
    identity_store.record_document(document_id="B", version="v1")
    identity_store.record_document(document_id="B", version="v2")
    identity_store.record_document(document_id="C", version="v2")

    resolved = identity_store.canonical_document_ids(["A", "B", "C"])
    assert resolved == {"A": "A", "B": "A", "C": "A"}

    with psycopg.connect(identity_database) as conn:
        claims = conn.execute(
            "SELECT version, document_id FROM fin_corpus_document_content "
            "ORDER BY version"
        ).fetchall()
    assert [tuple(row) for row in claims] == [("v1", "A"), ("v2", "A")]


def test_the_in_memory_store_keeps_one_owner_for_bytes_a_second_name_took() -> None:
    """The in-process store arbitrates exactly like the durable one."""
    store = InMemoryDocumentIdentityStore()

    store.record_document(document_id="A", version="v1")
    store.record_document(document_id="B", version="v1")
    store.record_document(document_id="B", version="v2")
    store.record_document(document_id="C", version="v2")

    assert store.canonical_document_ids(["A", "B", "C"]) == {
        "A": "A",
        "B": "A",
        "C": "A",
    }


def test_a_root_that_becomes_an_alias_resolves_through_the_chain(
    identity_database: str,
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """A root may become another root's alias; the chain collapses to its end.

    The alias row is written once and never rewritten, and the root's own
    earlier claim is not rewritten either: the mapping never renames
    anything. What changes is the RESOLUTION — the identifier, and the bytes
    it held first, now answer with the root at the end of the chain.
    """
    identity_store.record_document(document_id="A", version="v1")
    identity_store.record_document(document_id="B", version="v2")
    collapsed = identity_store.record_document(document_id="A", version="v2")
    later = identity_store.record_document(document_id="D", version="v1")

    assert collapsed.canonical_document_id == "B"
    assert later.canonical_document_id == "B"
    assert identity_store.canonical_document_ids(["A", "B", "D"]) == {
        "A": "B",
        "B": "B",
        "D": "B",
    }

    with psycopg.connect(identity_database) as conn:
        aliases = conn.execute(
            "SELECT alias_id, canonical_document_id "
            "FROM fin_corpus_document_alias ORDER BY alias_id"
        ).fetchall()
        claim = conn.execute(
            "SELECT document_id FROM fin_corpus_document_content "
            "WHERE version = 'v1'"
        ).fetchone()
    assert [tuple(row) for row in aliases] == [("A", "B"), ("D", "B")]
    # The earlier claim is not rewritten: the mapping renames nothing.
    assert claim is not None and claim[0] == "A"


def test_the_in_memory_store_collapses_the_same_chain() -> None:
    """The in-process store collapses a chain exactly like the durable one."""
    store = InMemoryDocumentIdentityStore()

    store.record_document(document_id="A", version="v1")
    store.record_document(document_id="B", version="v2")
    store.record_document(document_id="A", version="v2")
    store.record_document(document_id="D", version="v1")

    assert store.canonical_document_ids(["A", "B", "D"]) == {
        "A": "B",
        "B": "B",
        "D": "B",
    }


def test_a_looping_alias_chain_resolves_to_itself_and_is_logged(
    identity_database: str,
    identity_store: PostgresDocumentIdentityStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A damaged mapping must not hang a reader or name an arbitrary id.

    A loop in the alias table owns nothing, so the identifier answers with
    itself and the damage reaches the log.
    """
    with psycopg.connect(identity_database) as conn:
        conn.execute(
            "INSERT INTO fin_corpus_document_alias "
            "(alias_id, canonical_document_id) "
            "VALUES ('LOOP-A', 'LOOP-B'), ('LOOP-B', 'LOOP-A')"
        )
        conn.commit()

    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        assert identity_store.canonical_document_ids(["LOOP-A"]) == {
            "LOOP-A": "LOOP-A"
        }
        assert identity_store.canonical_document_ids(["LOOP-B"]) == {
            "LOOP-B": "LOOP-B"
        }

    assert any("LOOP-A" in record.getMessage() for record in caplog.records)


# ---------- a failure is visible, and it is not a data error ----------


class _UnavailableIdentityStore:
    """An identity store that cannot be reached, standing in for an outage."""

    def record_document(self, *, document_id: str, version: str) -> Any:
        raise DocumentIdentityStoreUnavailableError("the mapping store is down")

    def canonical_document_ids(self, document_ids: Any) -> dict[str, str]:
        raise DocumentIdentityStoreUnavailableError("the mapping store is down")


class _BrokenIdentityStore:
    """An identity store whose mapping call is itself wrong."""

    def record_document(self, *, document_id: str, version: str) -> Any:
        raise TypeError("the mapping call does not fit")

    def canonical_document_ids(self, document_ids: Any) -> dict[str, str]:
        return {}


class _FailingAfterFirstIdentityStore:
    """A store that records the first document and then cannot be reached."""

    def __init__(self) -> None:
        self._recorded = 0

    def record_document(
        self, *, document_id: str, version: str
    ) -> DocumentIdentityMapping:
        if self._recorded:
            raise DocumentIdentityStoreUnavailableError(
                "the mapping store is down"
            )
        self._recorded += 1
        return DocumentIdentityMapping(document_id, document_id, version)

    def canonical_document_ids(self, document_ids: Any) -> dict[str, str]:
        return {document_id: document_id for document_id in document_ids}


def test_an_unreachable_mapping_is_logged_and_the_reader_still_answers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A store outage is visible, and the reader still never raises.

    A degraded resolution silently reinstates the hole the mapping closes,
    so the outage has to reach the log even though the answer is still the
    caller's own id.
    """
    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        assert (
            canonical_document_id("AAPL-2024Q3", store=_UnavailableIdentityStore())
            == "AAPL-2024Q3"
        )
        assert canonical_document_ids(
            ["AAPL-2024Q3"], store=_UnavailableIdentityStore()
        ) == {"AAPL-2024Q3": "AAPL-2024Q3"}

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all("_UnavailableIdentityStore" in message for message in messages)
    assert all(
        "DocumentIdentityStoreUnavailableError" in message for message in messages
    )
    assert all(
        DocumentIdentityStoreUnavailableError.code in message
        for message in messages
    )


def test_an_identity_database_that_cannot_be_reached_leaves_the_id_unresolved(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The store is resolved INSIDE the guarded region, so no exception.

    Resolving the store builds it, and building it connects: an
    unreachable identity database failed the whole resolution before the
    guard could see it. The reader still answers with its own id, and
    the outage reaches the log.
    """
    monkeypatch.setenv(IDENTITY_DSN_ENV, UNREACHABLE_IDENTITY_DSN)
    reset_default_document_identity_store()

    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        assert canonical_document_id("AAPL-2024Q3") == "AAPL-2024Q3"
        assert canonical_document_ids(["AAPL-2024Q3"]) == {
            "AAPL-2024Q3": "AAPL-2024Q3"
        }

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 2
    assert all(
        "DocumentIdentityStoreUnavailableError" in message for message in messages
    )
    assert all(
        DocumentIdentityStoreUnavailableError.code in message
        for message in messages
    )


def test_a_defect_in_the_mapping_call_is_not_dressed_up_as_a_data_error() -> None:
    """A programming error surfaces as itself.

    The accept path turns a typed identity failure into a refusal, because
    the document is already durable by then. A defect in the call is not a
    data failure and must not be reported as one.
    """
    endpoint = _endpoint(identity_store=_BrokenIdentityStore())

    with pytest.raises(TypeError):
        endpoint.submit_durable(_BYTES, filename="team handbook.md")


# ---------- the accept refuses when the mapping cannot be written ----------


def _live_pointer(
    pointers: InMemoryMetadataStore,
    objects: InMemoryObjectStore,
    document_id: str,
) -> Any:
    """The document's pointer, when it still names bytes that exist."""
    try:
        pointer = pointers.get_pointer(document_id).pointer
    except MetadataPointerNotFoundError:
        return None
    try:
        objects.get(pointer.key)
    except UnknownObjectKeyError:
        return None
    return pointer


def test_a_mapping_that_cannot_be_written_refuses_the_accept() -> None:
    """The mapping write is the last thing that can refuse the accept.

    The document is already durable when the mapping is recorded, so the
    refusal takes the raw object back with it: an accepted document without
    its place in the mapping would resolve under one id only, which is the
    hole the mapping exists to close.
    """
    objects = InMemoryObjectStore()
    pointers = InMemoryMetadataStore()
    endpoint = _endpoint(
        identity_store=_UnavailableIdentityStore(),
        object_store=objects,
        metadata_store=pointers,
    )

    with pytest.raises(IngestDurableCommitError) as refused:
        endpoint.submit_durable(_BYTES, filename="team handbook.md")

    assert refused.value.code == "durable_commit_failed"
    assert refused.value.stage == "identity_record"
    document_id = _expected_generic_id(_BYTES)
    # The raw object of the refused accept is gone, and the document points
    # at no live bytes.
    with pytest.raises(UnknownObjectKeyError):
        objects.get(f"filings/{document_id}/{_VERSION}")
    assert _live_pointer(pointers, objects, document_id) is None
    # The pointer row itself is gone: a first accept has no prior pointer,
    # so nothing may keep naming the object the refusal deleted.
    with pytest.raises(MetadataPointerNotFoundError):
        pointers.get_pointer(document_id)


def test_a_mapping_that_cannot_be_written_restores_the_previous_pointer() -> None:
    """A refused replacement leaves the prior version exactly as it was.

    The pointer is overwritten before the mapping is recorded, so the
    compensating cleanup has to put the prior pointer back and delete this
    attempt's object: the document the caller already had stays accepted.
    """
    objects = InMemoryObjectStore()
    pointers = InMemoryMetadataStore()
    states = InMemoryIngestStateStore()
    endpoint = _endpoint(
        identity_store=_FailingAfterFirstIdentityStore(),
        object_store=objects,
        metadata_store=pointers,
        state_store=states,
    )
    first = endpoint.submit_durable(
        _BYTES, document_id="DOC-1", declared_format="md"
    )
    states.transition("DOC-1", IngestState.FAILED)

    with pytest.raises(IngestDurableCommitError) as refused:
        endpoint.submit_durable(
            _OTHER_BYTES, document_id="DOC-1", declared_format="md"
        )

    assert refused.value.stage == "identity_record"
    with pytest.raises(UnknownObjectKeyError):
        objects.get(
            f"filings/DOC-1/{hashlib.sha256(_OTHER_BYTES).hexdigest()}"
        )
    restored = pointers.get_pointer("DOC-1").pointer
    assert restored.key == f"filings/DOC-1/{first.version}"
    assert restored.sha256 == _VERSION
    assert objects.get(restored.key) == _BYTES


# ---------- one key enables the mapping ----------


def test_only_the_identity_dsn_enables_the_mapping(identity_database: str) -> None:
    """Another subsystem's DSN must not turn the mapping on.

    ``FIN_CHECKPOINT_URL`` is the process-wide database the hermetic test
    run bridges the test database onto. If it enabled the mapping, every
    accept would owe a second mandatory write to a database nobody asked
    for — and nothing would look at it, so the suite would stay green.
    """
    assert (
        document_identity_store_from_env({CHECKPOINT_DSN_ENV: identity_database})
        is None
    )
    # The identity key does enable it, so the answer above is about the
    # key, not about an unreachable database.
    store = document_identity_store_from_env({IDENTITY_DSN_ENV: identity_database})
    assert store is not None
    store.close()


def test_the_checkpoint_dsn_writes_no_mapping_row(
    identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With only the checkpoint DSN set, nothing is recorded and nothing fails.

    The mapping is opt-in through one key: the accept behaves exactly as it
    did before the mapping existed, and the mapping's own tables stay
    empty.
    """
    tables = PostgresDocumentIdentityStore(identity_database)
    tables.close()
    _wipe(identity_database)

    monkeypatch.delenv(IDENTITY_DSN_ENV, raising=False)
    monkeypatch.setenv(CHECKPOINT_DSN_ENV, identity_database)
    reset_default_document_identity_store()

    assert default_document_identity_store() is None
    _endpoint().submit_durable(_BYTES, filename="team handbook.md")

    with psycopg.connect(identity_database) as conn:
        aliases = conn.execute(
            "SELECT count(*) FROM fin_corpus_document_alias"
        ).fetchone()
        claims = conn.execute(
            "SELECT count(*) FROM fin_corpus_document_content"
        ).fetchone()
    assert aliases is not None and aliases[0] == 0
    assert claims is not None and claims[0] == 0


# ---------- the connect is bounded ----------


def test_the_connect_bound_defaults_to_five_seconds() -> None:
    """The store's connect attempt is bounded even with nothing configured.

    libpq waits without a bound of its own, so an identity host that
    drops the packets would hold a request path open. The store connects
    with a bound, and five seconds is the default.
    """
    assert DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS == 5
    assert identity_connect_timeout_seconds({}) == 5


def test_an_operator_can_name_a_different_connect_bound() -> None:
    """The bound is the operator's to set."""
    assert identity_connect_timeout_seconds({IDENTITY_CONNECT_TIMEOUT_ENV: "12"}) == 12


def test_an_unusable_connect_bound_degrades_to_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo does not remove the bound, and the operator is told.

    libpq reads zero and negative values as "wait forever", so an unusable
    override cannot be passed through: the default bound stands, and the
    warning names the setting that was ignored.
    """
    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        for raw in ("0", "-3", "soon", " "):
            assert (
                identity_connect_timeout_seconds({IDENTITY_CONNECT_TIMEOUT_ENV: raw})
                == 5
            )

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 3
    assert all(IDENTITY_CONNECT_TIMEOUT_ENV in message for message in messages)


def test_a_malformed_connect_bound_is_typed_and_degrades_to_the_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo is a typed failure, and the default bound still stands.

    The failure kind is machine-readable, so the operator sees WHICH
    setting was refused, and the public answer is unchanged: an unusable
    bound degrades to the default instead of taking the corpus door down.
    """
    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        assert (
            identity_connect_timeout_seconds({IDENTITY_CONNECT_TIMEOUT_ENV: "soon"})
            == DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS
        )

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert IDENTITY_CONNECT_TIMEOUT_ENV in messages[0]
    assert DocumentIdentityConnectBoundError.code in messages[0]


def test_every_identity_failure_carries_a_stable_code() -> None:
    """The failure kind is a code, not a token re-derived from a message.

    The code belongs to the class, so it cannot drift per call site.
    """
    assert DocumentIdentityError.code == "document_identity_error"
    assert DocumentIdentityConfigError.code == "identity_config_invalid"
    assert (
        DocumentIdentityStoreUnavailableError.code == "identity_store_unavailable"
    )
    assert (
        DocumentIdentityConnectBoundError.code == "identity_connect_bound_invalid"
    )


def test_the_store_connection_carries_the_connect_bound(
    identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bound reaches the store's OWN connection.

    The bound is read back from the live libpq connection the store
    holds, so the claim is about the connection that carries the traffic
    and not about the setting that was resolved.
    """
    monkeypatch.delenv(IDENTITY_CONNECT_TIMEOUT_ENV, raising=False)

    store = PostgresDocumentIdentityStore(identity_database)
    try:
        parameters = store._connection.info.get_parameters()
        assert parameters.get("connect_timeout") == "5"
    finally:
        store.close()


def test_the_operator_connect_bound_reaches_the_connection(
    identity_database: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What the operator named is what the connection carries."""
    monkeypatch.setenv(IDENTITY_CONNECT_TIMEOUT_ENV, "12")

    store = PostgresDocumentIdentityStore(identity_database)
    try:
        parameters = store._connection.info.get_parameters()
        assert parameters.get("connect_timeout") == "12"
    finally:
        store.close()


# ---------- the frozen formulas and the frozen outcomes ----------

def test_the_generic_identity_formula_is_unchanged() -> None:
    """Byte-identical id for the same scope and the same bytes."""
    endpoint = _endpoint()

    submission = endpoint.submit_durable(_BYTES, filename="team handbook.md")

    assert submission.document_id == _expected_generic_id(_BYTES)
    assert submission.document_id.startswith(CORPUS_DOCUMENT_ID_PREFIX)
    assert submission.version == _VERSION


def test_document_id_missing_still_fires_for_the_residual_case() -> None:
    """The typed outcome is retained, narrowed to the case that has no id.

    No explicit id and no derivable ``{TICKER}-{FYQ}`` pair: there is
    nothing to name the document with on the trusted explicit-ID seam, so
    the typed refusal is the answer. The public durable door cannot reach
    this case — it always mints the corpus-owned identity — so the residual
    refusal is pinned where it still fires.
    """
    endpoint = _endpoint()

    with pytest.raises(IngestDocumentIdMissingError):
        endpoint.ingest(_BYTES, declared_format="md")

    # The public door names the same upload itself instead of refusing.
    submission = endpoint.submit_durable(_BYTES, declared_format="md")
    assert submission.document_id == _expected_generic_id(_BYTES)


def test_a_corpus_owned_identifier_is_never_split_into_company_and_period() -> None:
    """The corpus-owned id carries no pair, so none is recovered from it.

    A corpus-owned id is derived from the scope and the bytes. Splitting it
    would invent a company and a period the id never encoded.
    """
    corpus_id = _expected_generic_id(_BYTES)

    assert split_company_period_from_document_id(corpus_id) == (None, None)
    # The financial shape still splits, so the guard did not break it.
    assert split_company_period_from_document_id("AAPL-2024Q3") == (
        "AAPL",
        "2024Q3",
    )


# ---------- the legacy projections are untouched ----------


def test_the_legacy_six_key_projection_is_unchanged(tmp_path: Path) -> None:
    """``GET /api/corpus`` keeps its six keys and its ids.

    The mapping adds resolution to a READ path; the compatibility
    projection for the existing financial rows must answer exactly as it
    did, key for key and id for id.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": "AAPL-2024Q3",
                "company": "AAPL",
                "period": "2024Q3",
                "version": _VERSION,
                "title": "Apple 10-Q",
                "doc_type": "narrative",
                "page_count": 3,
                "form": "10-Q",
            }
        ],
        catalog_path=catalog_path,
    )

    filings = list_filings(catalog_path=catalog_path)["filings"]

    assert filings == [
        {
            "document_id": "AAPL-2024Q3",
            "company": "AAPL",
            "period": "2024Q3",
            "version": _VERSION,
            "page_count": 3,
            "form": "10-Q",
        }
    ]
    assert set(filings[0].keys()) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }


def test_the_generic_read_model_and_the_legacy_projection_read_the_same_rows(
    tmp_path: Path,
) -> None:
    """Both catalog read models keep their own shape over the same table.

    The generic read model is the generic one; the legacy projection is the
    six-key financial one. Neither grows the other's keys.
    """
    catalog_path = tmp_path / "catalog.json"
    record_filings(
        [
            {
                "document_id": _expected_generic_id(_BYTES),
                "version": _VERSION,
                "title": "Team Handbook",
                "doc_type": "narrative",
                "page_count": 2,
            }
        ],
        catalog_path=catalog_path,
    )

    entries = list_catalog_entries(catalog_path=catalog_path)

    assert len(entries) == 1
    assert set(entries[0].keys()) == {
        "document_id",
        "title",
        "doc_type",
        "version",
        "page_count",
    }
    assert "company" not in entries[0]
    assert "period" not in entries[0]
    # The legacy projection still answers with its six keys.
    assert set(list_filings(catalog_path=catalog_path)["filings"][0].keys()) == {
        "document_id",
        "company",
        "period",
        "version",
        "page_count",
        "form",
    }


# ---------- the persistence is additive ----------


def test_the_identity_tables_are_new_and_leave_the_corpus_tables_alone(
    identity_database: str,
) -> None:
    """The mapping owns NEW tables; no existing table is altered.

    The corpus tables keep their columns and their rows: the mapping adds
    two tables of its own and touches nothing else.
    """
    store = PostgresDocumentIdentityStore(identity_database)
    try:
        with psycopg.connect(identity_database) as conn:
            alias_columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fin_corpus_document_alias'"
                ).fetchall()
            }
            content_columns = {
                row[0]
                for row in conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'fin_corpus_document_content'"
                ).fetchall()
            }
    finally:
        store.close()

    assert alias_columns == {
        "alias_id",
        "canonical_document_id",
        "created_at",
    }
    assert content_columns == {"version", "document_id", "created_at"}


# ---------- read point 1: the unit-text lookup ----------


def test_reading_by_the_alias_resolves_to_the_canonical_content(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The reader that holds the alias reads the canonical document's text.

    The alias and the canonical are ONE document, so both identifiers have
    to reach the same text — otherwise the alias is a name for nothing.
    """
    _alias_mapping(identity_store)
    pages = {
        CANONICAL_ID: (
            ExtractedPage(
                locator=Locator(
                    document_id=CANONICAL_ID, version=CORPUS_VERSION, unit=1
                ),
                text=DOC_TEXT,
                passages=(),
            ),
        )
    }

    assert (
        unit_text_from_pages(
            pages, ALIAS_ID, CORPUS_VERSION, 1, identity_store=identity_store
        )
        == DOC_TEXT
    )
    # The canonical id keeps answering exactly as it did.
    assert (
        unit_text_from_pages(
            pages, CANONICAL_ID, CORPUS_VERSION, 1, identity_store=identity_store
        )
        == DOC_TEXT
    )


def test_reading_by_an_unknown_identifier_is_still_a_miss(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """Resolution adds names; it never invents a document."""
    _alias_mapping(identity_store)
    pages = {
        CANONICAL_ID: (
            ExtractedPage(
                locator=Locator(
                    document_id=CANONICAL_ID, version=CORPUS_VERSION, unit=1
                ),
                text=DOC_TEXT,
                passages=(),
            ),
        )
    }

    assert (
        unit_text_from_pages(
            pages, "NOPE-2099Q9", CORPUS_VERSION, 1, identity_store=identity_store
        )
        is None
    )


def test_the_locator_shape_stays_exactly_document_id_version_unit() -> None:
    """The alias is resolved at the lookup, never by widening a locator.

    A locator is the closed triple ``{document_id, version, unit}``: an
    extra key would give the same frozen unit a second identity, so it is
    still refused — alias or no alias.
    """
    ok = validate_company_source_ref(
        {"document_id": CANONICAL_ID, "version": CORPUS_VERSION, "unit": 1}
    )
    assert ok == (CANONICAL_ID, CORPUS_VERSION, 1)

    extra = validate_company_source_ref(
        {
            "document_id": CANONICAL_ID,
            "version": CORPUS_VERSION,
            "unit": 1,
            "canonical_document_id": CANONICAL_ID,
        }
    )
    assert isinstance(extra, dict)
    assert extra["error"] == INVALID_LOCATOR


# ---------- read point 2: the access-scope filter ----------


def _chunk(document_id: str) -> Chunk:
    return Chunk(
        text=DOC_TEXT,
        source_ref=Locator(document_id=document_id, version=CORPUS_VERSION, unit=3),
        score=1.5,
    )


class _ScopeIgnoringPort(_FakeRetrievePort):
    """A company port that answers with its hits whatever the scope says.

    The HOST's narrowing is the unit under test here, so the port must not
    narrow first: a port that filtered on the scope would hide the host's
    behaviour behind its own.
    """

    def retrieve(self, query: str, **kwargs: Any) -> dict[str, Any]:
        kwargs.pop("document_scope", None)
        return super().retrieve(query, **kwargs)


def _port(document_id: str, *, ignoring_scope: bool = False) -> _FakeRetrievePort:
    port: _FakeRetrievePort = _FakeRetrievePort(
        hits_by_query={GROUNDED: [_chunk(document_id)]},
        known_document_ids={CANONICAL_ID, ALIAS_ID},
        manifest_docs={
            CANONICAL_ID: {"version": CORPUS_VERSION, "text": DOC_TEXT},
            ALIAS_ID: {"version": CORPUS_VERSION, "text": DOC_TEXT},
        },
    )
    if ignoring_scope:
        port.__class__ = _ScopeIgnoringPort
    return port


def _snapshot(document_id: str) -> dict[str, Any]:
    """The wire shape a Runtime-accepted reply commits to the checkpoint."""
    raw: dict[str, Any] = {
        "schema_version": 1,
        "mode": "restricted",
        "sources": [
            {
                "source_kind": "corpus",
                "source_id": document_id,
                "version": CORPUS_VERSION,
            }
        ],
        "accepted_input_version": 1,
    }
    SourceScopeSnapshot.from_state_dict(raw)
    return raw


def _compile(port: _FakeRetrievePort) -> Any:
    return compile_graph(
        checkpointer=InMemorySaver(),
        model=ScriptedChatModel(
            script=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "retrieve",
                            "args": {"query": GROUNDED},
                            "id": "c-retrieve",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="Reviewed."),
            ]
        ),
        adapter=port,
        known_document_ids={CANONICAL_ID, ALIAS_ID},
    )


def _run(graph: Any) -> dict[str, Any]:
    return graph.invoke(
        reset_input_state(GROUNDED),
        config={"configurable": {"thread_id": THREAD_ID}},
    )


def _commit(graph: Any, document_id: str) -> None:
    """Seed the accepted selection on the thread's own state channel.

    The write is a plain ``update_state`` with no ``as_node``: a snapshot
    must not itself model a turn, and the channel is the one the
    document-choice resume writes to.
    """
    graph._graph.update_state(
        {"configurable": {"thread_id": THREAD_ID}},
        {"source_scope": _snapshot(document_id)},
    )


def _tool_payload(out: dict[str, Any], name: str) -> dict[str, Any]:
    payloads = [
        json.loads(str(message.content))
        for message in out["messages"]
        if isinstance(message, ToolMessage) and getattr(message, "name", None) == name
    ]
    assert len(payloads) == 1, f"expected one {name} result: {payloads}"
    return payloads[0]


def test_the_scope_filter_keeps_the_canonical_hit_under_an_alias_selection(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The selection names the ALIAS; the hit carries the canonical id.

    Without the resolution the exact membership test drops a hit that
    belongs to the selected document, and the scope narrows to nothing
    without saying so. The hit has to survive.
    """
    _alias_mapping(identity_store)
    set_default_document_identity_store(identity_store)
    graph = _compile(_port(CANONICAL_ID))
    _commit(graph, ALIAS_ID)

    out = _run(graph)

    envelope = _tool_payload(out, "retrieve")
    assert [
        chunk["source_ref"]["document_id"] for chunk in envelope["chunks"]
    ] == [CANONICAL_ID]
    assert [item.locator.document_id for item in out["evidence"]] == [CANONICAL_ID]


def test_the_scope_filter_keeps_the_alias_hit_under_a_canonical_selection(
    identity_store: PostgresDocumentIdentityStore,
) -> None:
    """The selection names the CANONICAL id; the hit carries the alias.

    The other direction of the same hole: the hit is admitted under the
    alias the corpus stored it under, and it belongs to the selected
    document.
    """
    _alias_mapping(identity_store)
    set_default_document_identity_store(identity_store)
    graph = _compile(_port(ALIAS_ID, ignoring_scope=True))
    _commit(graph, CANONICAL_ID)

    out = _run(graph)

    envelope = _tool_payload(out, "retrieve")
    assert [
        chunk["source_ref"]["document_id"] for chunk in envelope["chunks"]
    ] == [ALIAS_ID]


def test_without_the_mapping_the_alias_selection_narrows_to_nothing() -> None:
    """The hole this guards, pinned: no mapping, the selection drops the hit.

    The scope names the alias and the corpus holds the document under its
    canonical id, so an exact membership test finds nothing. This is the
    behaviour the mapping closes, and it is what a regression would bring
    back.
    """
    graph = _compile(_port(CANONICAL_ID))
    _commit(graph, ALIAS_ID)

    out = _run(graph)

    envelope = _tool_payload(out, "retrieve")
    assert envelope["chunks"] == []


def test_an_unrelated_selection_still_narrows_the_hits() -> None:
    """The narrowing is unchanged for a selection that names another doc.

    Resolution widens nothing: a hit from a document outside the selection
    is still dropped.
    """
    store = InMemoryDocumentIdentityStore()
    _alias_mapping(store)
    set_default_document_identity_store(store)
    graph = _compile(_port(CANONICAL_ID))
    _commit(graph, "MSFT-2023Q1")

    out = _run(graph)

    envelope = _tool_payload(out, "retrieve")
    assert envelope["chunks"] == []
