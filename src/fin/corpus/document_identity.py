"""Corpus-owned document identity: the canonical id and its aliases.

One corpus document can be reached under more than one identifier. The same
bytes accepted through the generic leg and through the financial leg produce
two identifier forms for ONE document, and a reader that holds either one has
to find the same content.

The mapping is ADDITIVE: it only says "this id resolves to that canonical
id". It never renames an identifier, never re-derives one, never drops a
locator, a reader, a source hash, an access or retention scope, or a
tombstone. The canonical id stays the id the document is stored under, so
every existing table keeps applying to it unchanged.

* the CANONICAL id is the identifier the document was FIRST accepted under —
  the first writer wins, and it keeps every row it already has;
* an ALIAS is a second identifier for the same bytes, recorded when those
  bytes are accepted again under a different id;
* the mapping is MONOTONIC: a content version keeps the document that first
  held those bytes as its owner for good. A document that takes new bytes
  keeps its earlier claims, and a later document presenting already-claimed
  bytes becomes an alias of that first owner;
* a claim names the id that HOLDS the document: a second name for the same
  document never claims bytes of its own, and an alias chain resolves to
  the root at its end;
* resolution happens at the READ points, before an exact lookup, never by
  widening a locator or changing a formula.

The identity FORMULAS are frozen elsewhere and are not touched here:
``fin.corpus.upload_metadata.derive_document_identity`` owns the corpus-owned
generic id and ``fin.corpus.ingest_endpoint.resolve_document_id`` owns the
financial one. This module stores what those formulas produced; it derives
nothing.

Persistence is a NEW table pair in the same ``public`` schema the corpus
tables already use. Nothing here alters an existing table.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar, Final, LiteralString, Protocol, runtime_checkable

import psycopg

logger = logging.getLogger(__name__)

#: The prefix of a corpus-owned identifier (``derive_document_identity``).
#: A corpus-owned id is never split into company and period: it carries no
#: such pair, and a split would invent one.
CORPUS_DOCUMENT_ID_PREFIX: Final[str] = "doc_"

#: The environment key that names the database the identity tables live in.
#: It is the ONLY key: another subsystem's DSN must not silently turn the
#: mapping on, because the accept path then owes a second mandatory write to
#: a database nobody asked for. A deployment that wants the mapping names
#: it. ``FIN_CHECKPOINT_URL`` is deliberately NOT among them: it is the
#: process-wide database, and the hermetic test run bridges the test
#: database onto it.
IDENTITY_DSN_ENV: Final[str] = "FIN_DOCUMENT_IDENTITY_DSN"

#: The environment key that bounds the identity store's connect attempt.
#:
#: The mapping is ADDITIVE and OPTIONAL: an unreachable identity database
#: must degrade to "unresolved", not block the reader. libpq waits without
#: a bound of its own, so an identity host that drops the packets would
#: hold a request path open. This bound turns that wait into the typed
#: outage the read points already handle, and it is the ONE authority for
#: it: whatever the connection URL itself says, this is the timeout the
#: store connects with.
IDENTITY_CONNECT_TIMEOUT_ENV: Final[str] = (
    "FIN_DOCUMENT_IDENTITY_CONNECT_TIMEOUT_SECONDS"
)

#: The default connect bound, in seconds: long enough for a healthy
#: database on a slow link, short enough that a request path is not held.
DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS: Final[int] = 5

# The content claim: which document first held these bytes. The primary key
# on the version makes the claim single-writer, so the first accept wins.
# The claim is never released, so a version keeps exactly one owner.
#
# The alias mapping: a second identifier and the canonical id it resolves to.
# The primary key on the alias id makes the mapping single-writer too, so an
# identifier that already resolves keeps resolving to the same id.
_SCHEMA_STATEMENTS: Final[tuple[LiteralString, ...]] = (
    """
CREATE TABLE IF NOT EXISTS fin_corpus_document_content (
    version TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
    """
CREATE TABLE IF NOT EXISTS fin_corpus_document_alias (
    alias_id TEXT PRIMARY KEY,
    canonical_document_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
""",
)

#: How many alias rows one resolution may follow before the chain is treated
#: as unusable. A chain this long is a damaged mapping, not a document's
#: second name, and following it further would hold a read point open.
_MAX_ALIAS_HOPS: Final[int] = 32


class DocumentIdentityError(RuntimeError):
    """Base class for corpus document-identity failures.

    Every subclass owns a stable machine-readable ``code``, so a caller
    branches on the failure KIND without re-deriving a token from a message.
    """

    code: ClassVar[str] = "document_identity_error"


class DocumentIdentityConfigError(DocumentIdentityError):
    """The identity store was handed a configuration it cannot use."""

    code: ClassVar[str] = "identity_config_invalid"


class DocumentIdentityStoreUnavailableError(DocumentIdentityError):
    """The identity store could not be reached."""

    code: ClassVar[str] = "identity_store_unavailable"


class DocumentIdentityConnectBoundError(DocumentIdentityError):
    """The identity store was handed a connect bound it cannot use."""

    code: ClassVar[str] = "identity_connect_bound_invalid"


@dataclass(frozen=True, slots=True)
class DocumentIdentityMapping:
    """Where one identifier sits in the corpus identity mapping.

    ``canonical_document_id == alias_document_id`` means the identifier is
    the canonical one — the document is stored under it.
    """

    alias_document_id: str
    canonical_document_id: str
    version: str
    created_at: datetime | None = None

    @property
    def is_alias(self) -> bool:
        """Whether this identifier is a second name for another id."""
        return self.alias_document_id != self.canonical_document_id


@runtime_checkable
class DocumentIdentityStore(Protocol):
    """The corpus identity mapping, as a port.

    ``record_document`` is the accept-time write; ``canonical_document_ids``
    is the read-time resolution. Both answer in terms of identifiers only,
    so a reader never learns a second formula.
    """

    def record_document(
        self, *, document_id: str, version: str
    ) -> DocumentIdentityMapping:
        """Record an accepted document's place in the mapping."""
        ...

    def canonical_document_ids(
        self, document_ids: Sequence[str]
    ) -> Mapping[str, str]:
        """Map each identifier to the canonical id it resolves to."""
        ...


def _require_identifier(value: object, label: str) -> str:
    """The identifier itself, or a typed refusal.

    An identifier that is not a nonempty string names no document, so it is
    refused rather than stored as one.
    """
    if not isinstance(value, str) or not value.strip():
        raise DocumentIdentityConfigError(f"the {label} must be a nonempty string")
    return value


def _positive_connect_bound(raw: str) -> int:
    """The connect bound the operator named, or a typed refusal.

    A value that is not a positive whole number of seconds names no bound:
    libpq reads zero and negative values as "wait forever", which is
    exactly the unbounded wait this bound exists to remove.
    """
    unusable = (
        f"{IDENTITY_CONNECT_TIMEOUT_ENV}={raw!r} is not a positive whole "
        "number of seconds"
    )
    try:
        value = int(raw)
    except ValueError as exc:
        raise DocumentIdentityConnectBoundError(unusable) from exc
    if value <= 0:
        raise DocumentIdentityConnectBoundError(unusable)
    return value


def identity_connect_timeout_seconds(
    environ: Mapping[str, str] | None = None,
) -> int:
    """The connect bound for the identity database, in seconds.

    An override that is not a positive whole number of seconds degrades to
    the default rather than raising: a typo in an operator setting must
    not take the corpus door down, and the mapping is optional anyway.
    Zero and negative values are refused for a second reason: libpq reads
    them as "wait forever", which is exactly the unbounded wait this bound
    exists to remove.
    """
    env = os.environ if environ is None else environ
    raw = (env.get(IDENTITY_CONNECT_TIMEOUT_ENV) or "").strip()
    if not raw:
        # EXIT: no bound named — the default is the bound the store uses.
        return DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS
    try:
        return _positive_connect_bound(raw)
    except DocumentIdentityConnectBoundError as exc:
        # EXIT: an unusable bound degrades to the default — the mapping is
        # optional, and a typo must not take the corpus door down.
        logger.warning(
            "%s (code=%s); using %ds",
            exc,
            exc.code,
            DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS,
        )
        return DEFAULT_IDENTITY_CONNECT_TIMEOUT_SECONDS


def _resolve_alias_roots(
    document_ids: Sequence[str],
    look_up: Callable[[Sequence[str]], Mapping[str, str]],
) -> dict[str, str]:
    """Each identifier's ROOT canonical id — the end of its alias chain.

    ``look_up`` answers one hop at a time, in batches, so both backends walk
    a chain the same way and cannot disagree about which id is canonical. A
    self-alias or a loop names no owner: the identifier resolves to itself
    and the damaged mapping is logged rather than followed.
    """
    roots = {document_id: document_id for document_id in document_ids}
    paths = {document_id: [document_id] for document_id in document_ids}
    unsettled = list(document_ids)
    for _ in range(_MAX_ALIAS_HOPS):
        if not unsettled:
            return roots
        targets = look_up([paths[document_id][-1] for document_id in unsettled])
        still_unsettled: list[str] = []
        for document_id in unsettled:
            path = paths[document_id]
            target = targets.get(path[-1])
            if target is None:
                roots[document_id] = path[-1]
            elif target in path:
                # EXIT: a loop names no owner — the id stands for itself.
                logger.warning(
                    "the document identity alias chain of %r loops; it is "
                    "left unresolved",
                    document_id,
                )
            else:
                path.append(target)
                still_unsettled.append(document_id)
        unsettled = still_unsettled
    for document_id in unsettled:
        # EXIT: a chain longer than the bound is not followed — the id
        # stands for itself rather than resolving to an arbitrary id.
        logger.warning(
            "the document identity alias chain of %r is longer than %d hops; "
            "it is left unresolved",
            document_id,
            _MAX_ALIAS_HOPS,
        )
    return roots


class InMemoryDocumentIdentityStore:
    """The identity mapping held in this process.

    Same contract as the Postgres store, so a caller cannot tell which
    backend it was handed. It is process-local by nature: an alias recorded
    in one process is invisible to another, which is why a deployment that
    needs the mapping across processes names a database.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._claims: dict[str, str] = {}
        self._aliases: dict[str, str] = {}

    def _alias_targets(self, document_ids: Sequence[str]) -> dict[str, str]:
        return {
            document_id: self._aliases[document_id]
            for document_id in document_ids
            if document_id in self._aliases
        }

    def _roots(self, document_ids: Sequence[str]) -> dict[str, str]:
        return _resolve_alias_roots(list(document_ids), self._alias_targets)

    def record_document(
        self, *, document_id: str, version: str
    ) -> DocumentIdentityMapping:
        alias_id = _require_identifier(document_id, "document id")
        content_version = _require_identifier(version, "version")
        with self._lock:
            # The claim belongs to the id that HOLDS the document, so the
            # presented id is resolved to that root first: a second name for
            # the document must not claim new bytes of its own.
            root_id = self._roots([alias_id])[alias_id]
            holder = self._claims.get(content_version)
            if holder is None:
                self._claims[content_version] = root_id
                holder = root_id
            holder_root = self._roots([holder])[holder]
            if holder_root != root_id:
                # A second identifier for the same bytes: it resolves to the
                # root that holds them and is never renamed.
                self._aliases.setdefault(root_id, holder_root)
            canonical = self._roots([alias_id])[alias_id]
            return DocumentIdentityMapping(alias_id, canonical, content_version)

    def canonical_document_ids(
        self, document_ids: Sequence[str]
    ) -> Mapping[str, str]:
        with self._lock:
            return self._roots(document_ids)


class PostgresDocumentIdentityStore:
    """The identity mapping on real SQL rows.

    The tables are created idempotently in the same ``public`` schema the
    corpus tables use, so the mapping needs no migration of its own and
    changes no existing table.
    """

    def __init__(self, connection_url: str) -> None:
        if not isinstance(connection_url, str) or not connection_url.strip():
            raise DocumentIdentityConfigError(
                "the document identity store needs a PostgreSQL connection URL"
            )
        self._url = connection_url.strip()
        self._lock = threading.RLock()
        self._connect_timeout = identity_connect_timeout_seconds()
        try:
            self._connection = psycopg.connect(
                self._url, connect_timeout=self._connect_timeout
            )
        except psycopg.Error as exc:
            raise DocumentIdentityStoreUnavailableError(
                f"the document identity store could not connect ({type(exc).__name__})"
            ) from exc
        with self._transaction() as connection:
            for statement in _SCHEMA_STATEMENTS:
                connection.execute(statement)

    @contextmanager
    def _transaction(self) -> Iterator[psycopg.Connection[Any]]:
        """One serialised transaction on the store's own connection."""
        with self._lock:
            try:
                with self._connection.transaction():
                    yield self._connection
            except psycopg.Error as exc:
                raise DocumentIdentityStoreUnavailableError(
                    f"the document identity store could not be reached "
                    f"({type(exc).__name__})"
                ) from exc

    def close(self) -> None:
        """Close the store's connection."""
        with self._lock:
            self._connection.close()

    def _alias_targets(
        self, connection: Any, document_ids: Sequence[str]
    ) -> dict[str, str]:
        if not document_ids:
            return {}
        rows = connection.execute(
            "SELECT alias_id, canonical_document_id "
            "FROM fin_corpus_document_alias WHERE alias_id = ANY(%s)",
            (list(document_ids),),
        ).fetchall()
        return {str(row[0]): str(row[1]) for row in rows}

    def _roots(self, connection: Any, document_ids: Sequence[str]) -> dict[str, str]:
        return _resolve_alias_roots(
            list(document_ids),
            lambda ids: self._alias_targets(connection, ids),
        )

    def record_document(
        self, *, document_id: str, version: str
    ) -> DocumentIdentityMapping:
        alias_id = _require_identifier(document_id, "document id")
        content_version = _require_identifier(version, "version")
        with self._transaction() as connection:
            # The claim belongs to the id that HOLDS the document, so the
            # presented id is resolved to that root first: a second name for
            # the document must not claim new bytes of its own.
            root_id = self._roots(connection, [alias_id])[alias_id]
            holder = self._claim_holder(connection, root_id, content_version)
            holder_root = self._roots(connection, [holder])[holder]
            if holder_root != root_id:
                # A second identifier for the same bytes: it resolves to the
                # root that holds them and is never renamed.
                connection.execute(
                    "INSERT INTO fin_corpus_document_alias "
                    "(alias_id, canonical_document_id) VALUES (%s, %s) "
                    "ON CONFLICT (alias_id) DO NOTHING",
                    (root_id, holder_root),
                )
            canonical = self._roots(connection, [alias_id])[alias_id]
            row = connection.execute(
                "SELECT created_at FROM fin_corpus_document_alias "
                "WHERE alias_id = %s",
                (alias_id,),
            ).fetchone()
            if row is None:
                row = connection.execute(
                    "SELECT created_at FROM fin_corpus_document_content "
                    "WHERE version = %s AND document_id = %s",
                    (content_version, alias_id),
                ).fetchone()
            return DocumentIdentityMapping(
                alias_id,
                canonical,
                content_version,
                row[0] if row is not None else None,
            )

    def _claim_holder(
        self, connection: Any, claim_id: str, content_version: str
    ) -> str:
        """The identifier that owns these bytes, claiming them when free.

        The first writer of a version owns the canonical identity, so the
        claim is a single-writer insert and the holder is read back. The
        claim names the ROOT that holds the document, never an identifier
        that is itself an alias. A claim is never released: a document that
        takes new bytes keeps the ones it held first, and the bytes keep
        that one owner.
        """
        holder = connection.execute(
            "SELECT document_id FROM fin_corpus_document_content "
            "WHERE version = %s",
            (content_version,),
        ).fetchone()
        if holder is not None:
            return str(holder[0])
        connection.execute(
            "INSERT INTO fin_corpus_document_content "
            "(version, document_id) VALUES (%s, %s) "
            "ON CONFLICT (version) DO NOTHING",
            (content_version, claim_id),
        )
        claimed = connection.execute(
            "SELECT document_id FROM fin_corpus_document_content "
            "WHERE version = %s",
            (content_version,),
        ).fetchone()
        if claimed is None:
            raise DocumentIdentityStoreUnavailableError(
                "the document identity store could not record a content claim"
            )
        return str(claimed[0])

    def canonical_document_ids(
        self, document_ids: Sequence[str]
    ) -> Mapping[str, str]:
        ids = list(dict.fromkeys(document_ids))
        if not ids:
            return {}
        with self._transaction() as connection:
            roots = self._roots(connection, ids)
        return {document_id: roots[document_id] for document_id in ids}


class _Unset:
    """The marker for a default store that has not been resolved yet."""


_UNSET: Final[_Unset] = _Unset()
_default_store: DocumentIdentityStore | None | _Unset = _UNSET
_default_store_lock = threading.Lock()


def document_identity_store_from_env(
    environ: Mapping[str, str] | None = None,
) -> DocumentIdentityStore | None:
    """The identity store the environment names, or ``None``.

    ``None`` means no database was named, so there is no mapping to record
    or to resolve: the corpus behaves exactly as it did before the mapping
    existed.
    """
    env = os.environ if environ is None else environ
    url = (env.get(IDENTITY_DSN_ENV) or "").strip()
    if url:
        return PostgresDocumentIdentityStore(url)
    return None


def default_document_identity_store() -> DocumentIdentityStore | None:
    """The process-wide identity store, or ``None`` when none is configured.

    Resolved once from the environment, so every read point in the process
    sees the same mapping.
    """
    global _default_store
    with _default_store_lock:
        if isinstance(_default_store, _Unset):
            _default_store = document_identity_store_from_env()
        return _default_store


def set_default_document_identity_store(
    store: DocumentIdentityStore | None,
) -> None:
    """Point the process at one identity store (``None`` disables it).

    The wiring door for a boot that names the database itself, and the door
    a test uses to hand the read points a store.
    """
    global _default_store
    with _default_store_lock:
        _default_store = store


def reset_default_document_identity_store() -> None:
    """Go back to resolving the default store from the environment."""
    global _default_store
    with _default_store_lock:
        _default_store = _UNSET


def _resolved_store(
    store: DocumentIdentityStore | None,
) -> DocumentIdentityStore | None:
    if store is not None:
        return store
    return default_document_identity_store()


def _report_resolution_failure(
    store: DocumentIdentityStore | None, exc: DocumentIdentityError
) -> None:
    """Make an identity-database outage visible.

    The reader still answers, but a store failure and "no alias is known"
    must not look alike: the first is an outage, the second is an ordinary
    answer, and only the first needs an operator. The typed identity
    errors carry the failure kind, never a credential.

    ``store`` is ``None`` when the store could not even be BUILT: the
    environment named a database that could not be reached, and there is
    no instance to name.
    """
    if store is None:
        logger.warning(
            "the document identity store could not be resolved "
            "(error=%s, code=%s): %s",
            type(exc).__name__,
            exc.code,
            exc,
        )
        return
    logger.warning(
        "the document identity store could not resolve an identifier "
        "(store=%s, error=%s, code=%s): %s",
        type(store).__name__,
        type(exc).__name__,
        exc.code,
        exc,
    )


def canonical_document_id(
    document_id: str, *, store: DocumentIdentityStore | None = None
) -> str:
    """The canonical id for ``document_id`` — itself when no alias is known.

    Never raises. The mapping is additive, so an unreachable store leaves the
    id unresolved and the caller's exact lookup still answers; that is the
    behaviour the corpus had before the mapping existed. Resolving the store
    is inside the guard too: building it connects, and a database that
    cannot be reached must degrade the same way a query that fails does.
    """
    resolved: DocumentIdentityStore | None = None
    try:
        resolved = _resolved_store(store)
        if resolved is None:
            return document_id
        return resolved.canonical_document_ids([document_id]).get(
            document_id, document_id
        )
    except DocumentIdentityError as exc:
        # EXIT: an unreachable mapping leaves the id unresolved — the caller's
        # exact lookup still answers, and the outage is reported to the log.
        _report_resolution_failure(resolved, exc)
        return document_id


def canonical_document_ids(
    document_ids: Iterable[str], *, store: DocumentIdentityStore | None = None
) -> dict[str, str]:
    """Each identifier mapped to the canonical id it resolves to.

    Never raises, for the same reason :func:`canonical_document_id` does.
    """
    ids = [document_id for document_id in document_ids if document_id]
    resolved: DocumentIdentityStore | None = None
    try:
        resolved = _resolved_store(store)
        if resolved is None or not ids:
            return {document_id: document_id for document_id in ids}
        return dict(resolved.canonical_document_ids(ids))
    except DocumentIdentityError as exc:
        # EXIT: an unreachable mapping leaves every id unresolved — each id
        # resolves to itself, and the outage is reported to the log.
        _report_resolution_failure(resolved, exc)
        return {document_id: document_id for document_id in ids}
