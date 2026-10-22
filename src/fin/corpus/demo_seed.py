"""The ``seed:demo`` chain: guard -> reset -> schema -> embed probe ->
preview / ``--yes`` -> submit -> worker -> poll.

Scope and order
---------------

The chain is the executable half of ``specs/demo-seed.md`` change 5 (ADR 0100
decisions 3 and 4). Every step that can be decided from local inputs is decided
here; the two steps whose contract belongs to iknow-memory's ``specs/
demo-seed-dev-stack.md`` (the ``--reset`` data-table allowlist, the schema entry
point, and the ``index:reset-job`` command) sit behind
:class:`PlanADevStackAdapter`, which invokes the agreed npm script names and
fails loudly by name while they are unpublished. Nothing here writes to the
paired repository and no docker container or volume is touched.

Failure contract
----------------

Every refusal is a typed :class:`DemoSeedError` with a stable ``code``; there is
no silent fallback and no empty catch. The guard refusals — unset
``IKNOW_MEMORY_HOME``, the default memory home, a wrong port, a storage backend
that does not persist, an embedding generation mismatch — subclass
:class:`DemoSeedGuardError`, whose :attr:`~DemoSeedError.exit_code` is ``2``;
every other failure exits ``1``. The CLI maps one to the other, so the exit code
is a property of the failure class rather than of the print statements that
happen to run first.

Guard
-----

The guard is a pure function over a ``Mapping[str, str]``: it reads an injected
environment and an injected user home, and touches neither the filesystem nor a
socket. That is what makes every guard acceptance case unit-testable without a
dev stack. It runs twice — once at the head of the chain, and again immediately
before the worker subprocess starts, so a stack selection that changed in
between cannot put the worker on the main store.

Corpus rows
-----------

The catalog demo rows are **generated from the fixture**, one row per document,
with ``page_count`` and the document type taken from the fixture's own
metadata. The three hard-coded rows this module used to hold are
gone: the fixture directory is the single source for both the corpus that gets
pushed and the catalog rows that describe it, so the two cannot name different
documents. ``--reset`` clears exactly those rows, by that same document-id
set. Writing goes through :func:`fin.corpus.catalog.replace_filings`, the
catalog's own seam: the seed owns the rows of the documents the fixture holds
and replaces them, so a fixture whose bytes changed cannot leave the previous
version's row next to the new one. The rows of documents the seed does not own
-- the real corpus -- are never touched.

The wire ``source_ref``
-----------------------

One envelope per unit, the §B4 granularity: a unit is a page, so a
three-page document is three envelopes, three source refs and three index
jobs, and each envelope's body is that unit's body. One envelope per document
can only name one unit, so a hit on the second page would cite the first.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Protocol, runtime_checkable
from urllib.parse import urlparse

from fin.corpus.catalog import (
    _load_rows as _catalog_load_rows,
    _table_lock as _catalog_table_lock,
    _write_rows as _catalog_write_rows,
    list_filings,
    replace_filings,
)
from fin.corpus.parsers._common import UnitLocator
from fin.corpus.push import serialize_source_ref
from fin.retrieve.index_job_port import (
    IndexJobPort,
    IndexJobRecord,
    IndexJobUnknownSourceError,
)
from fin.retrieve.push_port import PushPort

#: The teardown reports on one logger. The seed CLI calls ``basicConfig`` so
#: that ``INFO`` here is visible under ``seed:demo``; without that call a library
#: logger would be dropped by the root handler and the one line that proves the
#: group is gone would never be printed.
logger: Final[logging.Logger] = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

#: The demo corpus, one ingest envelope per document. Read at runtime: the
#: document list is never written down here.
DEMO_FIXTURE_DIR: Final[Path] = (
    Path(__file__).resolve().parents[3] / "fixtures" / "demo-corpus"
)

#: The question set that travels with the fixture. It is not a document, so it
#: is excluded from the document set by name.
EXPECTED_FIXTURE_NAME: Final[str] = "expected.json"

#: ADR 0100 decision 3: the only ports ``seed:demo`` may target.
DEV_STACK_PORTS: Final[Mapping[str, int]] = MappingProxyType(
    {"postgres": 5434, "elasticsearch": 9201, "neo4j": 7688}
)

MEMORY_HOME_ENV: Final[str] = "IKNOW_MEMORY_HOME"
DEFAULT_MEMORY_HOME_NAME: Final[str] = ".iknow-memory"
DB_URL_ENV: Final[str] = "GRAPHRAG_MEMORY_DB_URL"
ES_URL_ENV: Final[str] = "GRAPHRAG_MEMORY_ES_URL"
NEO4J_URI_ENV: Final[str] = "GRAPHRAG_MEMORY_NEO4J_URI"

#: iknow's backend selector, read here and never written here: the MCP child
#: inherits this process environment whole, so this value is the one the service
#: resolves its storage from.
STORAGE_ENV: Final[str] = "GRAPHRAG_MEMORY_STORAGE"

#: What iknow resolves when STORAGE_ENV names none. It does not persist --
#: ``push`` answers ``written: 2`` for rows that reach no store, and the index
#: worker then drains with ``claimed: 0`` -- so an unset selector is a refusal
#: rather than a pass, exactly as an omitted port is.
NON_PERSISTENT_STORAGE: Final[str] = "memory"

#: The only backend this seed accepts: the one every fin acceptance run and
#: live suite configures against the dev Postgres.
PERSISTENT_STORAGE: Final[str] = "pgvector"

_PORT_ENV_BY_STORE: Final[Mapping[str, str]] = MappingProxyType(
    {
        "postgres": DB_URL_ENV,
        "elasticsearch": ES_URL_ENV,
        "neo4j": NEO4J_URI_ENV,
    }
)

#: The default port a store falls back to when its URL omits one. Both are the
#: production defaults, which is why an omitted port is a refusal and not a
#: pass: a target that never said where it is has not proven it is the dev one.
_DEFAULT_PORT_BY_STORE: Final[Mapping[str, int]] = MappingProxyType(
    {"postgres": 5432, "elasticsearch": 9200, "neo4j": 7687}
)

#: The version components ``computeContentIdentity`` uses when the caller names
#: none (iknow-memory ``src/tools/ingest.ts``,
#: ``DEFAULT_CONTENT_IDENTITY_VERSIONS``). They are service-owned: a caller
#: that hashed with its own versions would leave a row addressable by a pair the
#: other writer cannot reproduce.
CONTENT_IDENTITY_VERSIONS: Final[tuple[str, str, str]] = (
    "normalization-v1",
    "parse-v1",
    "parameters-v1",
)

#: The npm scripts the chain invokes in the paired repo, under the contract of
#: iknow-memory ``specs/demo-seed-dev-stack.md`` items 8 and 9.
PLAN_A_NPM_SCRIPTS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "worker": "index:worker",
        "reset_job": "index:reset-job",
        "schema": "migrate:dev",
    }
)

#: Where Plan A publishes the ``--reset`` data-table allowlist (its item 9,
#: "excluding the migration ledger"). Absent until Plan A ships it.
PLAN_A_TABLE_ALLOWLIST_FILE: Final[str] = "scripts/demo-seed-data-tables.json"

#: The synthetic issuer the catalog rows carry, matching the corpus the fixture
#: describes. A fixed company, not a fixed document list.
DEMO_ISSUER: Final[str] = "ZEUS"

# The SC20 filter surface of one pushed envelope. The demo corpus bypasses
# fin's parse/chunk pipeline -- it arrives pre-chunked in the fixture -- so the
# three rule versions name the fixture itself rather than a parser that never
# ran. They are non-empty, stable, and honest about what produced the text.
DEMO_NORMALIZATION_RULE_VERSION: Final[str] = "verbatim-v1"
DEMO_PARSE_RULE_VERSION: Final[str] = "demo-fixture-v1"
DEMO_PARAMS_TABLE_VERSION: Final[str] = "demo-fixture-v1"
DEMO_FIXTURE_FORMAT: Final[str] = "json"

#: How long a finished worker gets to exit on SIGTERM before it is killed.
WORKER_TERMINATE_TIMEOUT_SECONDS: Final[float] = 10.0

#: ``/proc/<pid>/stat`` process states that are not running: a zombie (``Z``) is
#: reaped by init and holds nothing, a dead (``X``) one is already gone.
_DEAD_STAT_STATES: Final[frozenset[str]] = frozenset({"Z", "X"})

#: How often the teardown re-enumerates the group while it drains. Short enough
#: that a worker dies promptly, long enough not to spin on ``/proc``.
_TEARDOWN_POLL_SECONDS: Final[float] = 0.05

#: The first run's poll budget (spec: set by the executing session and written
#: into the report). Later runs use a multiple of the measured per-source
#: maximum from the first ``seed:demo --reset --yes``.
DEFAULT_POLL_TIMEOUT_SECONDS: Final[float] = 1800.0
DEFAULT_POLL_INTERVAL_SECONDS: Final[float] = 2.0

_TERMINAL_FAILURE_STATUSES: Final[frozenset[str]] = frozenset({"failed", "stale"})
_READY_STATUS: Final[str] = "ready"
_PENDING_STATUS: Final[str] = "in_progress"

#: The breaker the spec names on failure; it is a ``failure_code`` value, and
#: it gets its own class because the operator action differs from a plain
#: extract failure.
ENTITY_MERGE_EMBED_LIMIT: Final[str] = "ENTITY_MERGE_EMBED_LIMIT"


# ---------------------------------------------------------------------------
# typed failures
# ---------------------------------------------------------------------------


class DemoSeedError(RuntimeError):
    """Base class for the demo seed's named failures.

    Every subclass carries a distinct stable :attr:`code` and an
    :attr:`exit_code`, so a caller classifies the failure without reading
    message text and the CLI never has to guess an exit code.
    """

    code: str = "demo_seed_error"
    exit_code: int = 1


class DemoSeedGuardError(DemoSeedError):
    """One of the guard refusals; every one exits ``2``."""

    code = "demo_seed_guard_failed"
    exit_code = 2


class DemoMemoryHomeUnsetError(DemoSeedGuardError):
    """``IKNOW_MEMORY_HOME`` is unset or blank -- no stack was selected."""

    code = "demo_seed_memory_home_unset"


class DemoDefaultMemoryHomeError(DemoSeedGuardError):
    """``IKNOW_MEMORY_HOME`` names the default ``$HOME/.iknow-memory``."""

    code = "demo_seed_default_memory_home"


class DemoPortMismatchError(DemoSeedGuardError):
    """A store target is not the isolated dev stack's port."""

    code = "demo_seed_port_mismatch"


class DemoNonPersistentStorageError(DemoSeedGuardError):
    """The storage backend the MCP resolved does not persist what it accepts."""

    code = "demo_seed_storage_not_persistent"


class DemoEmbeddingMismatchError(DemoSeedGuardError):
    """The stored embedding dimension or generation is not the declared one."""

    code = "demo_seed_embedding_mismatch"


class DemoFixtureMissingError(DemoSeedError):
    """The demo fixture directory does not exist."""

    code = "demo_seed_fixture_missing"


class DemoFixtureError(DemoSeedError):
    """A fixture file is not a usable ingest envelope."""

    code = "demo_seed_fixture_invalid"


class PlanAEntryPointError(DemoSeedError):
    """A paired-repo entry point (Plan A items 8 / 9) is absent or refused.

    Raised by name so the operator reads "Plan A has not shipped this" instead
    of an npm exit code that says nothing about which half is missing.
    """

    code = "demo_seed_plan_a_entry_point_missing"


class DemoWorkerError(DemoSeedError):
    """The ``index:worker`` process could not be started or reaped."""

    code = "demo_seed_worker_failed"


class DemoWorkerUnavailableError(DemoWorkerError):
    """The worker command is not an explicit path, or could not be spawned."""

    code = "demo_seed_worker_unavailable"


class DemoStoreUnavailableError(DemoSeedError):
    """A store the ``--reset`` needs could not be addressed."""

    code = "demo_seed_store_unavailable"


class DemoNeo4jUnavailableError(DemoStoreUnavailableError):
    """``MATCH (n) DETACH DELETE n`` has no client on the fin side.

    Neo4j is reachable only over bolt here, and fin ships no bolt driver, so
    the graph half of ``--reset`` is refused by name rather than silently
    skipped: a reset that cleared the tables and left the graph would look like
    it had worked.
    """

    code = "demo_seed_neo4j_client_unavailable"


class DemoJobFailedError(DemoSeedError):
    """A job reached a terminal state that is not ``ready``."""

    code = "demo_seed_job_failed"

    def __init__(
        self,
        message: str,
        *,
        job_id: str,
        failure_code: str | None,
        reset_command: Sequence[str],
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.failure_code = failure_code
        self.reset_command = list(reset_command)


class EmbedLimitBreachedError(DemoJobFailedError):
    """The job failed with ``ENTITY_MERGE_EMBED_LIMIT``.

    The named subclass of :class:`DemoJobFailedError` so the report can say
    which breaker tripped: this one is the embedding ceiling, not an extract
    failure, and the reset command is the same but the diagnosis is not.
    """

    code = "demo_seed_entity_merge_embed_limit"


class ReusedFailedJobError(DemoJobFailedError):
    """A submit joined an existing job that is already ``failed``.

    Reusing a failed job means the durable row still holds the failure, so the
    run stops before the worker starts and names ``index:reset-job`` -- the one
    command that makes the pair claimable again.
    """

    code = "demo_seed_reused_failed_job"


class DemoPollTimeoutError(DemoSeedError):
    """The poll budget expired with jobs still open."""

    code = "demo_seed_poll_timeout"

    def __init__(
        self,
        message: str,
        *,
        job_ids: Sequence[str],
        reset_command: Sequence[str],
    ) -> None:
        super().__init__(message)
        self.job_ids = tuple(job_ids)
        self.reset_command = list(reset_command)


# ---------------------------------------------------------------------------
# the typed result channel
# ---------------------------------------------------------------------------

#: The prefix of the one machine-readable line the seed prints when it
#: refuses. Both sides of the process boundary quote this constant -- the CLI
#: writes the line, a host route reads it -- so the format has exactly one
#: owner. The human-readable refusal line stays free-form prose: a caller
#: classifies by code, never by reading text.
SEED_RESULT_PREFIX: Final[str] = "[seed:demo] result "

#: The seed's guard refused the environment it was pointed at.
SEED_RESULT_REFUSED: Final[str] = "refused"

#: The seed ran and stopped on one of its named failures.
SEED_RESULT_FAILED: Final[str] = "failed"

_SEED_RESULT_STATUSES: Final[frozenset[str]] = frozenset(
    {SEED_RESULT_REFUSED, SEED_RESULT_FAILED}
)

#: The shape of a seed code: lowercase letters, digits and underscores. A
#: candidate that does not match was not minted by this library, so it is not
#: trusted on the other side of the process boundary.
_SEED_RESULT_CODE_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]*$")


def format_seed_result(status: str, code: str) -> str:
    """Build the typed result line for a refused or failed seed run.

    The status names which kind of stop happened and the code names which
    failure; both come from the seed's own vocabulary, so the caller never
    has to read prose to classify the run.
    """
    if status not in _SEED_RESULT_STATUSES:
        raise ValueError(f"unknown seed result status: {status!r}")
    if not _SEED_RESULT_CODE_PATTERN.match(code):
        raise ValueError(f"malformed seed result code: {code!r}")
    return f"{SEED_RESULT_PREFIX}status={status} code={code}"


def parse_seed_result(output: str) -> tuple[str, str] | None:
    """Read the seed's typed result line back out of captured output.

    Returns ``(status, code)`` for the last well-formed result line, or
    ``None`` when the output carries no typed result -- an untyped crash, for
    instance. A line that does not parse is ignored rather than trusted: the
    typed channel is the only thing that crosses the process boundary as
    data.
    """
    parsed: tuple[str, str] | None = None
    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line.startswith(SEED_RESULT_PREFIX):
            continue
        fields: dict[str, str] = {}
        for field in line[len(SEED_RESULT_PREFIX) :].split():
            name, separator, value = field.partition("=")
            if separator:
                fields[name] = value
        status = fields.get("status", "")
        code = fields.get("code", "")
        if status in _SEED_RESULT_STATUSES and _SEED_RESULT_CODE_PATTERN.match(code):
            parsed = (status, code)
    return parsed


# ---------------------------------------------------------------------------
# content identity -- the service-owned formula
# ---------------------------------------------------------------------------


def content_identity_of(content: str) -> str:
    """The ``content_identity`` the service stores for an envelope body.

    This is a port of iknow-memory's ``computeContentIdentity``
    (``src/tools/ingest.ts``, the ``ContentIdentityVersions`` block): sha256
    over ``JSON.stringify([content, normalization, parse, parameters])`` with the
    service's own default version components. ``JSON.stringify`` of a
    four-string array emits exactly the compact, non-ASCII-escaped form
    reproduced by ``json.dumps(..., separators=(",", ":"), ensure_ascii=False)``,
    so the digest matches byte for byte.

    fin computes this from the fixture rather than echoing a push response,
    because the demo corpus is pushed pre-chunked and the identity is what
    ``submit_index_job`` is addressed by. Inventing a second formula here would
    produce a pair the service cannot resolve.
    """
    payload = json.dumps(
        [content, *CONTENT_IDENTITY_VERSIONS],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# fixture
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoDocument:
    """One fixture document: an ingest envelope plus what the catalog needs.

    ``page_count``, ``document_type`` and ``form`` are the fixture's own values,
    not constants -- a row that disagrees with the document it names is worse
    than no row. ``form`` is the EDGAR form (``10-K`` / ``8-K`` / ``10-Q``) and
    is what the catalog's ``form`` column carries: that column is filtered by
    exact match (ADR 0031), so a row that put ``document_type`` there would
    answer ``form="10-K"`` with ``[]``. ``document_type`` still reaches the push
    envelope as ``doc_type``.

    ``document_id`` is the document's identity -- the fixture's file stem,
    which the fixture must also declare as its ``source_ref`` -- and the
    document is the catalog's unit, not the wire's: the wire unit is the page,
    and :attr:`units` holds one :class:`DemoUnit` per page. The document's
    whole body is not kept: the body the seed pushes is the unit's, and a
    document-level body would only invite a caller to use it by mistake.
    """

    document_id: str
    title: str
    chunks: tuple[Mapping[str, Any], ...]
    metadata: Mapping[str, Any]
    valid_from: str
    source_sha256: str

    @property
    def locator_version(self) -> str:
        """The version every unit of this document is cited by.

        One authority for the locator version: the pushed ``source_ref``, the
        catalog row and the host's allowlist all read this string, so they
        cannot name different versions of the same document.
        """
        return self.source_sha256

    @property
    def units(self) -> tuple[DemoUnit, ...]:
        """The document's frozen units, one per page, in page order.

        The chunk's 1-based ordinal is the unit: the fixture holds one chunk
        per page, and the loader refuses an index below zero, so the ordinal
        is always 1-based. The chunk's heading names the page, and the two
        must agree -- the web resolves a citation only when the source's page
        is the citation's unit, so a fixture whose heading and ordinal
        disagree would push a locator nothing can resolve.
        """
        return _units_of(self)

    @property
    def document_type(self) -> str:
        return str(self.metadata["document_type"])

    @property
    def form(self) -> str:
        """The EDGAR form the catalog row is filtered by.

        Refused here rather than defaulted, because the catalog filters this
        column by exact match: a row carrying no form is a row nothing can
        filter for. Validated at row-build time, not at load time, so a
        document that never reaches the catalog does not have to declare one.
        """
        form = self.metadata.get("form")
        if not (isinstance(form, str) and form.strip()):
            raise DemoFixtureError(
                f"fixture document {self.document_id!r} declares form {form!r} in "
                "metadata; it must be a non-empty EDGAR form (10-K / 8-K / 10-Q) "
                "because the catalog filters that column by exact match -- the "
                "seed will not invent one"
            )
        return form

    @property
    def page_count(self) -> int:
        return int(self.metadata["page_count"])


@dataclass(frozen=True)
class DemoUnit:
    """One frozen unit of a demo document: one page, one chunk.

    The demo's unit IS the page. The fixture holds one chunk per page and the
    chunk's 1-based ordinal is the page number, so the locator the seed pushes
    and the locator the host's allowlist resolves are one identity.

    ``version`` is the document's own content hash -- the sha256 of the fixture
    file, one per document, exactly as the ingest endpoint computes it for a
    real upload -- so re-seeding identical content yields an identical
    ``source_ref`` and the push stays idempotent.
    """

    document: DemoDocument
    unit: int
    chunk: Mapping[str, Any]

    @property
    def locator(self) -> UnitLocator:
        """The §B4 locator triple this unit is cited by."""
        return UnitLocator(
            document_id=self.document.document_id,
            version=self.document.locator_version,
            unit=self.unit,
        )

    @property
    def page(self) -> int:
        """The page the chunk's heading names.

        The heading is the fixture's own page marker (``Title · Page N``); it
        is the axis beside the unit, and the seed refuses a fixture whose two
        disagree -- see :attr:`DemoDocument.units`.
        """
        return _page_of(self.document.document_id, self.document.title, self.content)

    @property
    def source_ref(self) -> str:
        """The wire ``source_ref``: the locator triple's stable serialization."""
        return serialize_source_ref(self.locator)

    @property
    def content(self) -> str:
        """The unit's text: the fixture's page, verbatim."""
        return str(self.chunk["content"])

    @property
    def content_identity(self) -> str:
        """The identity the service computes for this body.

        The service's own formula (:func:`content_identity_of`), not fin's
        :func:`fin.corpus.push.content_identity` over the envelope's declared
        rule versions. The two disagree for this envelope on purpose -- see
        :meth:`push_envelope`.
        """
        return content_identity_of(self.content)

    def push_envelope(self) -> dict[str, Any]:
        """The wire envelope handed to the push port.

        The body and the slice ride verbatim: fin does not re-split a fixture
        (``不二次切分``). The SC20 filter surface is filled from the fixture
        where it carries the field and derived otherwise, because the adapter
        refuses an envelope whose metadata is incomplete. ``source_ref`` is the
        §B4 canonical serialization -- the value the service echoes back and
        fin's hit mapper reads -- and it names this unit, so a hit on this
        page cites this page.

        ``body_content_id`` is the **service's** hash of this body
        (:func:`content_identity_of`), deliberately not fin's
        :func:`fin.corpus.push.body_content_id` over the envelope's declared
        rule versions: the demo declares the fixture's rule versions
        (``verbatim-v1`` / ``demo-fixture-v1``) while the service keys its
        row by its own defaults, and the index job is addressed by the pair
        the service resolves. The two formulas therefore disagree for this
        envelope; changing the value here would change the port's idempotency
        key.
        """
        metadata = dict(self.document.metadata)
        metadata.update(
            {
                "fmt": str(metadata.get("fmt") or DEMO_FIXTURE_FORMAT),
                "doc_type": self.document.document_type,
                "section": str(metadata.get("section") or self.document.title),
                "source_sha256": self.document.source_sha256,
                "parse_rule_version": str(
                    metadata.get("parse_rule_version")
                    or DEMO_PARSE_RULE_VERSION
                ),
                "params_table_version": str(
                    metadata.get("params_table_version")
                    or DEMO_PARAMS_TABLE_VERSION
                ),
                "normalization_rule_version": str(
                    metadata.get("normalization_rule_version")
                    or DEMO_NORMALIZATION_RULE_VERSION
                ),
                "document_type": self.document.document_type,
                "page_count": self.document.page_count,
                "locator": dict(self.locator.to_dict()),
            }
        )
        return {
            "source_ref": self.source_ref,
            "content": self.content,
            "chunks": [{"content": self.content, "index": 0}],
            "metadata": metadata,
            "valid_from": self.document.valid_from,
            "body_content_id": self.content_identity,
        }


def _require(condition: bool, document_id: str, detail: str) -> None:
    if not condition:
        raise DemoFixtureError(f"fixture document {document_id!r} {detail}")


def _document_of(path: Path) -> DemoDocument:
    """Read and validate one fixture file as an ingest envelope."""
    document_id = path.stem
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise DemoFixtureError(
            f"fixture document {document_id!r} is not readable JSON: {exc}"
        ) from exc
    _require(isinstance(payload, Mapping), document_id, "is not a JSON object")
    body = payload.get("source_ref")
    _require(
        isinstance(body, str) and bool(body.strip()),
        document_id,
        "carries no source_ref",
    )
    _require(
        body == document_id,
        document_id,
        f"declares source_ref {body!r}; the file name is the document id, so "
        "the two must agree",
    )
    # The fixture's whole body is a format requirement, not a body the seed
    # pushes: the pushed body is the unit's. The check stays so a fixture
    # that lost its body is still a named refusal.
    whole_body = payload.get("content")
    _require(
        isinstance(whole_body, str) and bool(whole_body.strip()),
        document_id,
        "carries no content body",
    )
    chunks = _chunks_of(payload.get("chunks"), document_id)
    metadata = payload.get("metadata")
    _require(isinstance(metadata, Mapping), document_id, "carries no metadata")
    page_count = metadata.get("page_count")
    _require(
        not isinstance(page_count, bool)
        and isinstance(page_count, int)
        and page_count >= 0,
        document_id,
        f"declares page_count {page_count!r}; it must be a non-negative int",
    )
    document_type = metadata.get("document_type")
    _require(
        isinstance(document_type, str) and bool(document_type.strip()),
        document_id,
        "declares no document_type in metadata",
    )
    # The catalog's `form` column is the EDGAR form and is filtered by exact
    # match, so a missing form is a malformed CATALOG ROW, not a malformed
    # document. It is required by DemoDocument.form at row-build time rather
    # than here: loading a document must not demand a field only the catalog
    # consumes, or the seed chain could not run against a document set that is
    # not seeding a library.
    title = payload.get("title")
    _require(
        isinstance(title, str) and bool(title.strip()),
        document_id,
        "carries no title",
    )
    valid_from = payload.get("valid_from")
    _require(
        isinstance(valid_from, str) and bool(valid_from.strip()),
        document_id,
        "carries no valid_from",
    )
    document = DemoDocument(
        document_id=body,
        title=title,
        chunks=chunks,
        metadata=dict(metadata),
        valid_from=valid_from,
        source_sha256=_file_sha256(path),
    )
    # Deriving the units refuses a fixture whose page heading and chunk
    # ordinal disagree, so an unusable fixture is named at load time rather
    # than at the first push.
    _units_of(document)
    return document


def _chunks_of(value: Any, document_id: str) -> tuple[Mapping[str, Any], ...]:
    _require(
        isinstance(value, list) and bool(value),
        document_id,
        "carries no chunks; the envelope must hold at least one slice",
    )
    chunks: list[Mapping[str, Any]] = []
    for index, chunk in enumerate(value):
        _require(
            isinstance(chunk, Mapping), document_id, f"chunk #{index} is not an object"
        )
        _require(
            isinstance(chunk.get("content"), str)
            and bool(str(chunk["content"]).strip()),
            document_id,
            f"chunk #{index} carries no content",
        )
        _require(
            not isinstance(chunk.get("index"), bool)
            and isinstance(chunk.get("index"), int)
            and chunk["index"] >= 0,
            document_id,
            f"chunk #{index} carries index {chunk.get('index')!r}; it must be a "
            "non-negative int, because the chunk's 1-based ordinal is the unit "
            "the locator names",
        )
        chunks.append(dict(chunk))
    return tuple(chunks)


#: The fixture's page marker: a chunk's first line names the page it is.
_PAGE_HEADER = re.compile(r"^.+ · Page ([1-9][0-9]*)$")


def _page_of(document_id: str, title: str, content: str) -> int:
    """The page the chunk's heading names; refuse a heading that names none.

    The heading is the fixture's own page marker (``Title · Page N``). It is
    the axis beside the unit -- the unit is the chunk's ordinal -- and the
    seed checks the two against each other, so the locator it pushes names
    the page the host resolves.
    """
    lines = content.splitlines()
    match = _PAGE_HEADER.fullmatch(lines[0]) if lines else None
    if match is None or lines[0] != f"{title} · Page {match.group(1)}":
        raise DemoFixtureError(
            f"fixture document {document_id!r} has an invalid page heading"
        )
    return int(match.group(1))


def _units_of(document: DemoDocument) -> tuple[DemoUnit, ...]:
    """The document's units, with the page/ordinal correspondence checked.

    The one place the unit is derived: the chunk's 1-based ordinal. The
    chunk's heading names the page, and the two must be the same page --
    the wire's unit and the host's page are one axis, so a fixture whose
    two disagree would push a locator the web cannot resolve.
    """
    units = tuple(
        DemoUnit(document=document, unit=int(chunk["index"]) + 1, chunk=chunk)
        for chunk in document.chunks
    )
    for unit in units:
        if unit.page != unit.unit:
            raise DemoFixtureError(
                f"fixture document {document.document_id!r} chunk {unit.unit} "
                f"heads page {unit.page}; the chunk's 1-based ordinal is the "
                "unit the locator names, so the two must agree"
            )
    return units


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:  # pragma: no cover - unreadable fixture file
        raise DemoFixtureError(f"fixture file {path} is unreadable: {exc}") from exc


def load_demo_documents(
    fixture_dir: Path | str | None = None,
) -> tuple[DemoDocument, ...]:
    """Every fixture document in ``fixture_dir``, ordered by document id.

    The set is read from the directory rather than declared, so adding a
    document is a fixture change and never a code change. ``expected.json`` is
    the question set, not a document, and is excluded by name.
    """
    directory = Path(fixture_dir) if fixture_dir is not None else DEMO_FIXTURE_DIR
    if not directory.is_dir():
        raise DemoFixtureMissingError(
            f"demo fixture directory {directory} does not exist; the demo "
            "corpus is read from the fixture, never hard-coded"
        )
    paths = sorted(
        path
        for path in directory.glob("*.json")
        if path.name != EXPECTED_FIXTURE_NAME
    )
    if not paths:
        raise DemoFixtureMissingError(
            f"demo fixture directory {directory} holds no document files"
        )
    return tuple(_document_of(path) for path in paths)


# ---------------------------------------------------------------------------
# catalog rows, generated from the fixture
# ---------------------------------------------------------------------------


def _catalog_row(document: DemoDocument) -> dict[str, Any]:
    """One catalog row per document.

    ``version`` is the document's own content hash -- the sha256 of the fixture
    file, the same string the pushed ``source_ref`` carries as its locator
    version (:attr:`DemoDocument.locator_version`, the one authority for it).
    The catalog's version slot names the content version, and the row's
    ``(document_id, version)`` is therefore the locator triple the retrieval
    side hands back, so a hit can resolve its document in the catalog and the
    two can never diverge.

    ``form`` is the document's EDGAR ``metadata.form``, never ``document_type``:
    the catalog filters that column by exact match (ADR 0031), so it has to hold
    the form an operator would type (``10-K`` / ``8-K`` / ``10-Q``). Both values
    come from the fixture, so neither is a constant here.

    ``title`` and ``doc_type`` are the T6 row contract's required display
    fields: the demo document's own ``metadata.document_type`` is the type,
    and the ``source_ref`` is its title (the seed predates the ingested
    ``title``; a fixture with no display name is named by its locator rather
    than invented).
    """
    return {
        "document_id": document.document_id,
        "title": document.title,
        "doc_type": document.document_type,
        "company": str(document.metadata.get("company") or DEMO_ISSUER),
        "period": str(
            document.metadata.get("period") or document.valid_from[:10]
        ),
        "version": document.locator_version,
        "page_count": document.page_count,
        "form": document.form,
        "cik": str(document.metadata.get("cik") or "0000000042"),
        "ticker": str(document.metadata.get("ticker") or DEMO_ISSUER),
        "filing_date": str(
            document.metadata.get("filing_date") or document.valid_from[:10]
        ),
    }


def demo_catalog_rows(
    fixture_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """The catalog rows the demo corpus describes, one per document."""
    return [_catalog_row(document) for document in load_demo_documents(fixture_dir)]


def seed_demo_catalog(
    catalog_path: Path | str | None = None,
    *,
    fixture_dir: Path | str | None = None,
) -> tuple[int, Path | None]:
    """Write the fixture-derived demo rows; return ``(rows_written, path)``.

    The seed owns the rows of the documents the fixture holds, so it
    *replaces* them: the catalog upserts on ``(document_id, version)`` and a
    fixture whose bytes changed carries a new version, so an ordinary upsert
    would leave the previous version's row next to the new one -- one demo
    document visible twice in ``list_filings``. The rows of documents the seed
    does not own -- the real corpus -- are never touched.

    ``catalog_path`` defaults to ``None``, which lets the catalog resolve
    the development catalog location -- the seed command targets the development
    library, not a per-test temp file. Tests pass an explicit ``tmp_path`` so a
    routine run never touches the development catalog.
    """
    rows = demo_catalog_rows(fixture_dir)
    written = replace_filings(
        rows,
        document_ids=[row["document_id"] for row in rows],
        catalog_path=catalog_path,
    )
    resolved = Path(catalog_path) if catalog_path is not None else None
    return written, resolved


def clear_demo_catalog_rows(
    catalog_path: Path | str | None,
    *,
    document_ids: Sequence[str],
) -> int:
    """Remove the demo rows whose ``document_id`` is in ``document_ids``.

    The catalog only upserts, so a clear has to rewrite the table. It
    reuses the catalog module's own lock, row loader and atomic writer rather
    than opening a second manifest writer: the on-disk format, the flock
    discipline and the temp-file rewrite stay single-sourced in
    :mod:`fin.corpus.catalog`. Rows outside ``document_ids`` -- the real corpus,
    including the frozen AAPL rows -- are carried through untouched.
    """
    targets = set(document_ids)
    path = Path(catalog_path if catalog_path is not None else _default_catalog_path())
    with _catalog_table_lock(path):
        rows = _catalog_load_rows(path)
        kept = [row for row in rows if row["document_id"] not in targets]
        removed = len(rows) - len(kept)
        if removed:
            _catalog_write_rows(path, kept)
    return removed


def _default_catalog_path() -> Path:
    from fin.corpus.catalog import DEFAULT_CATALOG_PATH

    return DEFAULT_CATALOG_PATH


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------


def _port_of(store: str, raw_url: str) -> int:
    """The port a store URL addresses, defaulting as that store defaults."""
    try:
        parsed = urlparse(raw_url)
        port = parsed.port
    except ValueError as exc:
        raise DemoPortMismatchError(
            f"{_PORT_ENV_BY_STORE[store]} is not a URL this guard can parse: "
            f"{raw_url!r} ({exc})"
        ) from exc
    return port if port is not None else _DEFAULT_PORT_BY_STORE[store]


def _guarded_memory_home(
    environ: Mapping[str, str],
    *,
    user_home: Path | str | None,
) -> str:
    raw = environ.get(MEMORY_HOME_ENV)
    if raw is None or not raw.strip():
        raise DemoMemoryHomeUnsetError(
            f"{MEMORY_HOME_ENV} is not set; seed:demo refuses to guess which "
            "iknow-memory stack it would write to"
        )
    selected = Path(raw.strip())
    default_home = (
        Path(user_home) if user_home is not None else Path.home()
    ) / DEFAULT_MEMORY_HOME_NAME
    if selected == default_home or _same_path(selected, default_home):
        raise DemoDefaultMemoryHomeError(
            f"{MEMORY_HOME_ENV} points at the default directory {default_home}; "
            "seed:demo writes demo data and only ever targets the isolated "
            "dev stack"
        )
    return str(selected)


def _same_path(left: Path, right: Path) -> bool:
    """Whether two paths name the same directory, resolved and normalized.

    A refusal that only compared strings would pass a target that reached the
    default home by a different spelling, which is the case the guard exists for.
    """
    try:
        return left.resolve() == right.resolve()
    except OSError:  # pragma: no cover - unresolvable path
        return False


def _persistent_storage(environ: Mapping[str, str]) -> str:
    """The storage backend the MCP child resolves; refuse one that drops data.

    ``STORAGE_ENV`` is read from the same mapping the three store URLs come
    from, which is the seed process environment the MCP child inherits whole
    and an explicit setting wins over the memory home's own config. An unset
    selector is a refusal, not a pass: iknow then resolves its non-persistent
    ``memory`` default, so ``push`` would report rows written that no store
    holds and the index worker would claim nothing.
    """
    raw = environ.get(STORAGE_ENV, "").strip()
    if raw == PERSISTENT_STORAGE:
        return raw
    if not raw:
        raise DemoNonPersistentStorageError(
            f"{STORAGE_ENV} is not set, so the MCP resolves its "
            f"{NON_PERSISTENT_STORAGE!r} storage backend, which does not "
            f"persist: push would report rows written that no store holds and "
            f"the index worker would claim none. Export "
            f"{STORAGE_ENV}={PERSISTENT_STORAGE} for the demo stack."
        )
    raise DemoNonPersistentStorageError(
        f"{STORAGE_ENV} resolves the MCP to the {raw!r} storage backend; "
        f"{PERSISTENT_STORAGE!r} is the only backend this seed has proven "
        f"persists, and on any other one push would report rows written that "
        f"no store holds and the index worker would claim none. Export "
        f"{STORAGE_ENV}={PERSISTENT_STORAGE} for the demo stack."
    )


def guard_dev_stack(
    environ: Mapping[str, str],
    *,
    user_home: Path | str | None = None,
) -> str:
    """Refuse anything but the isolated dev stack; return the memory home.

    Requires ``IKNOW_MEMORY_HOME`` to be set, to not be the default
    ``$HOME/.iknow-memory``, every store target to sit on the dev port
    (Postgres 5434, Elasticsearch 9201, Neo4j 7688), and the resolved storage
    backend to persist. Pure: it reads the mapping and the injected home, and
    opens nothing.
    """
    memory_home = _guarded_memory_home(environ, user_home=user_home)
    for store, expected_port in DEV_STACK_PORTS.items():
        variable = _PORT_ENV_BY_STORE[store]
        raw_url = environ.get(variable)
        if raw_url is None or not raw_url.strip():
            raise DemoPortMismatchError(
                f"{variable} is not set, so the {store} target the isolated "
                f"dev stack would use (port {expected_port}) is unproven"
            )
        observed = _port_of(store, raw_url)
        if observed != expected_port:
            raise DemoPortMismatchError(
                f"{variable} addresses port {observed}, not the dev port "
                f"{expected_port}; seed:demo refuses to write outside the "
                "isolated dev stack"
            )
    _persistent_storage(environ)
    return memory_home


# ---------------------------------------------------------------------------
# embed probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmbedGeneration:
    """What the stack's ``embedding_generation`` singleton currently holds."""

    dimensions: int
    generation: str


@runtime_checkable
class EmbedProbePort(Protocol):
    """Reads the singleton the stack indexes vectors against."""

    def read_embedding_generation(self) -> EmbedGeneration:  # pragma: no cover
        ...


def probe_embedding_generation(
    probe: EmbedProbePort,
    *,
    expected_dimensions: int,
    expected_generation: str,
) -> EmbedGeneration:
    """Check the stored dimension and generation; refuse a mismatch.

    The model and dimension are one fixed value on both sides (spec
    「Interface contract」), so a mismatch means the stack was built for a
    different embedding than this seed declares -- which would surface much
    later as an unexplained vector error. It is a guard refusal, so it exits 2.
    """
    observed = probe.read_embedding_generation()
    if (
        observed.dimensions != expected_dimensions
        or observed.generation != expected_generation
    ):
        raise DemoEmbeddingMismatchError(
            f"the dev stack stores embedding generation {observed.generation!r} "
            f"at {observed.dimensions} dimensions, but this seed declares "
            f"{expected_generation!r} at {expected_dimensions} dimensions; "
            "indexing against a different generation is refused"
        )
    return observed


# ---------------------------------------------------------------------------
# paired-repo entry points (Plan A)
# ---------------------------------------------------------------------------


@runtime_checkable
class PlanAEntryPoints(Protocol):
    """The paired-repo half of the chain, as the chain uses it."""

    def apply_schema(self) -> None:  # pragma: no cover - protocol
        ...

    def data_table_allowlist(self) -> tuple[str, ...]:  # pragma: no cover
        ...

    def reset_job_command(self, job_id: str) -> list[str]:  # pragma: no cover
        ...


class PlanADevStackAdapter:
    """The single adapter for the two Plan A-dependent steps.

    ``specs/demo-seed.md`` names three things as defined by Plan A: the
    ``index:reset-job`` command, the schema entry point, and the ``--reset``
    data-table allowlist. All three are invoked here, by the npm script names
    Plan A agreed (``index:reset-job``, ``migrate:dev``), against the paired
    repository -- which fin never writes to. While a script is absent the
    adapter refuses by name
    (:class:`PlanAEntryPointError`) rather than running a command that would
    fail with an exit code naming nothing.
    """

    def __init__(
        self,
        iknow_root: Path | str,
        *,
        environ: Mapping[str, str] | None = None,
        runner: Callable[[Sequence[str], Path, Mapping[str, str]], int] | None = None,
        allowlist_path: Path | str | None = None,
    ) -> None:
        self._root = Path(iknow_root)
        self._environ = dict(os.environ if environ is None else environ)
        self._runner = runner if runner is not None else _run_npm
        self._allowlist_path = (
            Path(allowlist_path)
            if allowlist_path is not None
            else self._root / PLAN_A_TABLE_ALLOWLIST_FILE
        )

    # ----- introspection -----

    def declared_scripts(self) -> Mapping[str, str]:
        """The npm scripts the paired repository declares, by name."""
        manifest = self._root / "package.json"
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlanAEntryPointError(
                f"{manifest} is not readable JSON, so the paired repository's "
                f"npm scripts cannot be confirmed: {exc}"
            ) from exc
        scripts = payload.get("scripts") if isinstance(payload, Mapping) else None
        if not isinstance(scripts, Mapping):
            raise PlanAEntryPointError(
                f"{manifest} declares no scripts object"
            )
        return {str(name): str(command) for name, command in scripts.items()}

    def missing_entry_points(self) -> tuple[str, ...]:
        """The Plan A scripts this seed needs that are not declared yet."""
        declared = self.declared_scripts()
        return tuple(
            script
            for script in (PLAN_A_NPM_SCRIPTS["reset_job"], PLAN_A_NPM_SCRIPTS["schema"])
            if script not in declared
        )

    def require_entry_points(self) -> None:
        missing = self.missing_entry_points()
        if missing:
            raise PlanAEntryPointError(
                f"the paired repository at {self._root} does not declare "
                f"{', '.join(missing)}; these are iknow-memory Plan A items 8 "
                "and 9 (specs/demo-seed-dev-stack.md) and must land before "
                "seed:demo can run for real"
            )

    # ----- commands -----

    def reset_job_command(self, job_id: str) -> list[str]:
        """The ``index:reset-job --job-id <id>`` command the spec prints."""
        return [
            "npm",
            "run",
            PLAN_A_NPM_SCRIPTS["reset_job"],
            "--",
            "--job-id",
            job_id,
        ]

    def schema_command(self) -> list[str]:
        """The dev schema entry point (Plan A item 9)."""
        return ["npm", "run", PLAN_A_NPM_SCRIPTS["schema"]]

    def _run(self, command: Sequence[str], *, what: str) -> None:
        try:
            returncode = self._runner(command, self._root, self._environ)
        except OSError as exc:
            raise PlanAEntryPointError(
                f"{what} could not be started ({type(exc).__name__}: {exc}); "
                f"command: {' '.join(command)} run from {self._root}"
            ) from exc
        if returncode != 0:
            raise PlanAEntryPointError(
                f"{what} exited {returncode}; command: {' '.join(command)} "
                f"(run from {self._root})"
            )

    def apply_schema(self) -> None:
        """Run the dev schema entry point (Plan A item 9)."""
        self.require_entry_points()
        self._run(self.schema_command(), what=PLAN_A_NPM_SCRIPTS["schema"])

    def reset_job(self, job_id: str) -> None:
        """Reset one job so the pair becomes claimable again (Plan A item 8)."""
        self.require_entry_points()
        self._run(
            self.reset_job_command(job_id), what=PLAN_A_NPM_SCRIPTS["reset_job"]
        )

    def data_table_allowlist(self) -> tuple[str, ...]:
        """Plan A's ``--reset`` data-table allowlist (Plan A item 9).

        The migration ledger is excluded by the contract itself; this adapter
        does not re-derive the list, because a second copy of it would be a
        second answer to "what may a reset truncate". ``allowlist_path`` takes
        the path Plan A publishes it at, so landing that file under a different
        name is a construction argument, not a code change here.
        """
        path = self._allowlist_path
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlanAEntryPointError(
                f"the --reset data-table allowlist is not published at {path}; "
                "iknow-memory Plan A item 9 defines it, and until it lands "
                "there is no table list this seed may truncate"
            ) from exc
        tables = payload.get("tables") if isinstance(payload, Mapping) else None
        if not isinstance(tables, list) or not tables or not all(
            isinstance(table, str) and bool(table.strip()) for table in tables
        ):
            raise PlanAEntryPointError(
                f"{path} carries no non-empty 'tables' list of table names"
            )
        return tuple(str(table) for table in tables)


def _run_npm(
    command: Sequence[str],
    cwd: Path,
    environ: Mapping[str, str],
) -> int:
    """Run one npm script and return its exit code."""
    completed = subprocess.run(  # noqa: S603 - argv built from agreed script names
        list(command),
        cwd=str(cwd),
        env=dict(environ),
        check=False,
    )
    return int(completed.returncode)


# ---------------------------------------------------------------------------
# storage reset
# ---------------------------------------------------------------------------


@runtime_checkable
class DevStackResetPort(Protocol):
    """The storage half of ``--reset``, addressed by table and store name.

    The allowlist is not asked for here: it is Plan A's, and the chain reads it
    through :class:`PlanADevStackAdapter` before handing it over.
    """

    def truncate_tables(  # pragma: no cover - protocol
        self, tables: tuple[str, ...]
    ) -> tuple[str, ...]:
        ...

    def delete_elasticsearch_indices(self) -> tuple[str, ...]:  # pragma: no cover
        ...

    def clear_neo4j_graph(self) -> tuple[str, ...]:  # pragma: no cover
        ...


@dataclass(frozen=True)
class ResetOutcome:
    """What ``--reset`` actually did, per store."""

    tables: tuple[str, ...]
    indices: tuple[str, ...]
    nodes_deleted: tuple[str, ...]
    catalog_rows_removed: int


def reset_dev_stack(
    *,
    reset_port: DevStackResetPort,
    plan_a: PlanAEntryPoints,
    catalog_path: Path | str | None,
    document_ids: Sequence[str],
) -> ResetOutcome:
    """Truncate the allowlisted tables, drop the ES indices, clear Neo4j.

    Only rows and documents are touched: no docker container and no volume is
    addressed anywhere on this path, which is what makes ``--reset`` safe to
    run against the running dev stack.
    """
    tables = plan_a.data_table_allowlist()
    truncated = tuple(reset_port.truncate_tables(tables))
    indices = tuple(reset_port.delete_elasticsearch_indices())
    nodes = tuple(reset_port.clear_neo4j_graph())
    removed = clear_demo_catalog_rows(catalog_path, document_ids=document_ids)
    return ResetOutcome(
        tables=truncated,
        indices=indices,
        nodes_deleted=nodes,
        catalog_rows_removed=removed,
    )


# ----- concrete store seams -----

_TABLE_NAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_]*$")


def _quoted_table(name: str) -> str:
    """A table name quoted for ``TRUNCATE``, after refusing anything else.

    An identifier cannot be a bound parameter, so the allowlist text is checked
    rather than escaped: a name that is not a plain lowercase identifier is
    refused by name instead of being interpolated into DDL.
    """
    if not _TABLE_NAME_PATTERN.match(name):
        raise DemoStoreUnavailableError(
            f"table {name!r} in the --reset allowlist is not a plain lowercase "
            "identifier; it is refused rather than quoted into DDL"
        )
    return f'"{name}"'


class PostgresStoreReset:
    """``TRUNCATE`` over the dev PostgreSQL, through an injected connection.

    Only the tables the allowlist names are truncated -- no ``CASCADE``, so a
    table the allowlist forgot keeps its rows rather than being emptied through a
    foreign key.
    """

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def truncate_tables(self, tables: tuple[str, ...]) -> tuple[str, ...]:
        statement = "TRUNCATE TABLE " + ", ".join(
            _quoted_table(table) for table in tables
        )
        try:
            with self._connect() as connection:  # type: ignore[operator]
                with connection.cursor() as cursor:
                    cursor.execute(statement)
                connection.commit()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed failure
            raise DemoStoreUnavailableError(
                f"truncating {len(tables)} table(s) failed "
                f"({type(exc).__name__}: {exc}); statement: {statement}"
            ) from exc
        return tuple(tables)


class PostgresEmbedProbe:
    """Read the dev stack's ``embedding_generation`` singleton.

    ``active_dimensions`` / ``active_model`` are the two values the stored
    vectors belong to, which is what the probe compares against the declared
    model. The row is read, never written.
    """

    _QUERY: Final[str] = (
        "SELECT active_dimensions, active_model FROM embedding_generation "
        "WHERE id = 1"
    )

    def __init__(self, connect: Callable[[], Any]) -> None:
        self._connect = connect

    def read_embedding_generation(self) -> EmbedGeneration:
        try:
            with self._connect() as connection:  # type: ignore[operator]
                with connection.cursor() as cursor:
                    cursor.execute(self._QUERY)
                    row = cursor.fetchone()
        except Exception as exc:  # noqa: BLE001 - re-raised as a typed failure
            raise DemoStoreUnavailableError(
                f"reading the embedding_generation singleton failed "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        if row is None:
            raise DemoStoreUnavailableError(
                "the dev stack has no embedding_generation singleton; run the "
                "schema entry point and boot the backend before seeding"
            )
        dimensions, model = row
        if (
            isinstance(dimensions, bool)
            or not isinstance(dimensions, int)
            or not isinstance(model, str)
            or not model
        ):
            raise DemoStoreUnavailableError(
                f"embedding_generation carries active_dimensions="
                f"{dimensions!r} and active_model={model!r}, which is not a "
                "readable generation"
            )
        return EmbedGeneration(dimensions=dimensions, generation=model)


class ElasticsearchIndexReset:
    """Delete every index on the dev Elasticsearch, through an injected opener.

    The index set is read from ``/_cat/indices`` rather than assumed, so a
    reset cannot leave an index the stack created under a name this module did
    not know about.
    """

    def __init__(
        self,
        base_url: str,
        *,
        opener: Callable[[str, str], tuple[int, bytes]] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._opener = opener if opener is not None else _http_request

    def index_names(self) -> tuple[str, ...]:
        status, body = self._request("GET", "/_cat/indices?format=json")
        if status != 200:
            raise DemoStoreUnavailableError(
                f"listing Elasticsearch indices at {self._base_url} returned "
                f"HTTP {status}: {body.decode(errors='replace')[:200]}"
            )
        try:
            payload = json.loads(body)
        except ValueError as exc:
            raise DemoStoreUnavailableError(
                f"the Elasticsearch index listing is not JSON: {exc}"
            ) from exc
        if not isinstance(payload, list):
            raise DemoStoreUnavailableError(
                "the Elasticsearch index listing is not a list"
            )
        return tuple(
            str(entry["index"])
            for entry in payload
            if isinstance(entry, Mapping) and entry.get("index")
        )

    def delete_elasticsearch_indices(self) -> tuple[str, ...]:
        deleted: list[str] = []
        for name in self.index_names():
            status, body = self._request("DELETE", f"/{_quoted_index(name)}")
            if status not in (200, 404):
                raise DemoStoreUnavailableError(
                    f"deleting Elasticsearch index {name!r} returned HTTP "
                    f"{status}: {body.decode(errors='replace')[:200]}"
                )
            deleted.append(name)
        return tuple(deleted)

    def _request(self, method: str, path: str) -> tuple[int, bytes]:
        try:
            return self._opener(method, f"{self._base_url}{path}")
        except OSError as exc:
            raise DemoStoreUnavailableError(
                f"Elasticsearch at {self._base_url} could not be reached "
                f"({type(exc).__name__}: {exc})"
            ) from exc


_INDEX_NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


def _quoted_index(name: str) -> str:
    if not _INDEX_NAME_PATTERN.match(name):
        raise DemoStoreUnavailableError(
            f"Elasticsearch index {name!r} is not a plain index name; it is "
            "refused rather than interpolated into a request path"
        )
    return name


def _http_request(method: str, url: str) -> tuple[int, bytes]:
    """One plain HTTP request; returns ``(status, body)``.

    A non-2xx answer is a value here, not an exception, so the caller can name
    the status; a transport failure surfaces as ``OSError``.
    """
    request = urllib.request.Request(url, method=method)  # noqa: S310 - dev URLs only
    try:
        with urllib.request.urlopen(request) as response:  # noqa: S310
            return int(response.status), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read()


class Neo4jGraphReset:
    """Refuse the graph half of ``--reset``: fin has no bolt client.

    The wipe itself is one statement -- ``MATCH (n) DETACH DELETE n`` -- but it
    needs a bolt driver fin does not ship, and writing one here would be a
    protocol client nobody asked for. The refusal is typed and explicit so the
    run stops instead of resetting two thirds of the stack.
    """

    def __init__(self, *, uri: str | None = None) -> None:
        self._uri = uri

    def clear_neo4j_graph(self) -> tuple[str, ...]:
        raise DemoNeo4jUnavailableError(
            "the Neo4j half of --reset needs a bolt client fin does not ship; "
            f"run 'MATCH (n) DETACH DELETE n' against {self._uri or DEV_STACK_PORTS['neo4j']} "
            "directly, or add the driver, before using seed:demo --reset"
        )


# ---------------------------------------------------------------------------
# worker supervision
# ---------------------------------------------------------------------------


def worker_command() -> list[str]:
    """``npm run index:worker -- --batch-size 1``.

    ``--batch-size 1`` is the spec's pacing: one job per claim, so a failure
    stops the run at a known source instead of mid-batch.
    """
    return [
        "npm",
        "run",
        PLAN_A_NPM_SCRIPTS["worker"],
        "--",
        "--batch-size",
        "1",
    ]


@runtime_checkable
class WorkerHandle(Protocol):
    """A worker process fin starts under the guard and then reaps.

    ``start`` is part of this contract, not only of
    :class:`SubprocessWorker`: the chain calls it on whatever
    ``worker_factory`` returns. A handle declaring only ``pid`` and
    ``terminate_and_wait`` would satisfy every structural check here and then
    raise ``AttributeError`` at run time, once the run had already submitted
    its jobs -- so the protocol states the whole lifecycle its consumer uses.

    ``open_jobs`` is keyword-only with a default so that the count the teardown
    reports is additive: a handle written against the one-argument contract
    still type-checks, and still passes a ``TypeError`` at run time if it is
    ever handed a keyword it does not accept -- which the tests hold it to.
    """

    pid: int

    def start(self, *, memory_home: str) -> int:  # pragma: no cover
        ...

    def terminate_and_wait(
        self, *, timeout_seconds: float, open_jobs: int = 0
    ) -> int:  # pragma: no cover
        ...


class SubprocessWorker:
    """One ``index:worker`` child process under fin's supervision.

    The command is started with an explicit path and the validated
    ``IKNOW_MEMORY_HOME`` passed in the child environment, so the worker reads
    the same stack the guard approved.

    The process is started in its own session, and the teardown signals the
    whole group rather than the pid. ``npm`` does not exec into the worker: it
    spawns a shell, which spawns ``node``, so signalling npm alone leaves the
    worker alive, reparented to init, still claiming jobs after the seed exits
    (observed in the wild -- the survivor showed ``ppid`` of ``/init``). A
    process that ignores SIGTERM past the timeout is killed; anything still in
    the group afterwards fails the run by name rather than being reported as a
    clean reap that did not happen.
    """

    def __init__(
        self,
        command: Sequence[str],
        *,
        cwd: Path | str,
        environ: Mapping[str, str],
    ) -> None:
        self._command = list(command)
        self._cwd = Path(cwd)
        self._environ = dict(environ)
        self._process: subprocess.Popen[bytes] | None = None
        self._pgid: int | None = None
        self.pid = -1
        self.environ: dict[str, str] = {}

    def start(self, *, memory_home: str) -> int:
        """Spawn the worker with ``memory_home`` as its stack selection."""
        self._require_explicit_command()
        self.environ = {**self._environ, MEMORY_HOME_ENV: memory_home}
        try:
            self._process = subprocess.Popen(  # noqa: S603 - argv is a fixed literal
                self._command,
                cwd=str(self._cwd),
                env=self.environ,
                start_new_session=True,
            )
        except OSError as exc:
            raise DemoWorkerUnavailableError(
                f"index:worker could not be started ({type(exc).__name__}: "
                f"{exc}); command: {' '.join(self._command)}"
            ) from exc
        self.pid = self._process.pid
        self._pgid = self._group_id(self.pid)
        return self.pid

    @staticmethod
    def _group_id(pid: int) -> int | None:
        """The process group the pid belongs to, or ``None`` if it is gone."""
        try:
            return os.getpgid(pid)
        except (ProcessLookupError, PermissionError):
            return None

    def _require_explicit_command(self) -> None:
        if not self._command:
            raise DemoWorkerUnavailableError("no index:worker command configured")
        program = self._command[0]
        if os.path.isabs(program) or os.sep in program:
            return
        raise DemoWorkerUnavailableError(
            f"index:worker must be started through an explicit command path, "
            f"not the bare program name {program!r}: the worker runs from the "
            "paired repository's npm script, so a PATH lookup would resolve a "
            "binary this seed never validated"
        )

    def terminate_and_wait(self, *, timeout_seconds: float, open_jobs: int = 0) -> int:
        """End the worker's process group and return the worker's own status.

        SIGTERM the group, wait out ``timeout_seconds``, then SIGKILL whatever is
        left of it; the group is then enumerated, and any survivor fails the run
        with its pid named. A survivor after SIGKILL is a process that left the
        group -- but it is exactly what must not pass unnoticed, and the pid is
        what an operator needs.

        ``open_jobs`` is reported only: the jobs this run left open are the
        operator's clue for what a worker that outlived the teardown was
        claiming.

        Idempotent. A process that never started raises; one that already exited
        is not signalled again, but its group still is -- npm exiting while node
        keeps running is the real shape of this bug, and skipping the group on
        ``poll() is not None`` left that orphan running. A group that is gone is
        success, not a failure.
        """
        process = self._process
        if process is None:
            raise DemoWorkerError("index:worker was never started")
        pgid = self._pgid if self._pgid is not None else process.pid
        sent: str | None = None
        # The child already being gone does NOT mean the group is: npm exiting
        # while node keeps running is the real shape of this bug, and signalling
        # only on ``poll() is None`` left that orphan running. The group is what
        # is ended; the child's own status is only read afterwards.
        if process.poll() is None or _process_group_members(pgid):
            sent = self._signal_group(pgid, signal.SIGTERM)
            if not self._wait_for_exit(timeout_seconds):
                sent = self._signal_group(pgid, signal.SIGKILL)
                self._wait_for_exit(timeout_seconds)
        survivors = self._survivors(pgid)
        logger.info(
            "[seed:demo] worker teardown worker_pgid=%s signal=%s "
            "open_jobs=%s survivors=%s",
            pgid,
            sent or "none",
            open_jobs,
            "[" + ", ".join(str(pid) for pid in survivors) + "]",
        )
        if survivors:
            raise DemoWorkerError(
                f"index:worker left {len(survivors)} process(es) running after "
                f"SIGTERM and SIGKILL on process group {pgid}: "
                f"pids {', '.join(str(pid) for pid in survivors)}; a worker that "
                "outlives the seed keeps claiming jobs, so the run fails rather "
                "than reports a reap it cannot prove"
            )
        return int(process.returncode if process.returncode is not None else 0)

    def _signal_group(self, pgid: int, sig: signal.Signals) -> str | None:
        """Signal the group; ``None`` when the group had already gone away."""
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return None
        except PermissionError as exc:
            raise DemoWorkerError(
                f"process group {pgid} could not be signalled ({exc}); it is not "
                "this run's to end, so the worker cannot be reaped"
            ) from exc
        return sig.name

    def _wait_for_exit(self, timeout_seconds: float) -> bool:
        """Wait for the group to empty and the child to be reaped.

        Waiting on the child alone is not the group's exit: npm exits long
        before node does, and ``process.wait`` returning would then leave the
        worker still running with nobody signalling it any more.

        The child's own status is read only once the group is empty. A
        zero-timeout ``wait`` can succeed on a child the kernel has not finished
        reaping while the group still holds other members, and returning its
        half-set ``returncode`` would hand back a status that changes on the next
        call.
        """
        process = self._process
        assert process is not None  # only called after the None check
        deadline = time.monotonic() + max(timeout_seconds, 0.0)
        while _process_group_members(self._pgid_of(process)):
            if time.monotonic() >= deadline:
                return False
            time.sleep(_TEARDOWN_POLL_SECONDS)
        try:
            process.wait(timeout=_TEARDOWN_POLL_SECONDS)
        except subprocess.TimeoutExpired:  # pragma: no cover - child still there
            return False
        return True

    def _pgid_of(self, process: subprocess.Popen[bytes]) -> int:
        return self._pgid if self._pgid is not None else process.pid

    def _survivors(self, pgid: int) -> tuple[int, ...]:
        """The pids still alive in the group, this process excluded."""
        return tuple(
            pid for pid in _process_group_members(pgid) if pid != os.getpid()
        )


def _pid_alive(pid: int) -> bool:
    """True while the kernel still has a live process with this pid.

    ``kill(pid, 0)`` cannot tell a zombie from a running process -- both answer
    -- so liveness is read from ``/proc``, where a zombie is a state rather than
    a process. A zombie holds no memory, no socket and no job claim; reporting
    one as a survivor would fail runs over a process that is already dead, and
    one of them does appear in practice: a grandchild that exited between the
    signals stays in the group as an orphan zombie until init reaps it.
    """
    fields = _stat_fields(pid)
    return bool(fields) and fields[0] not in _DEAD_STAT_STATES


def _process_group_members(pgid: int) -> tuple[int, ...]:
    """Every live pid whose process group is ``pgid``, read from ``/proc``.

    ``killpg(pgid, 0)`` only proves the group is non-empty; this is the member
    list the teardown reports. Linux-only by construction -- ``/proc/<pid>/stat``
    field 5 is the process group id, and there is no ``/proc`` on a platform
    without it.
    """
    own_group = os.getpgid(0)
    if pgid == own_group or pgid == os.getpid():
        # Signalling would reach this process and every side process fin shares
        # the group with (an MCP stdio child, an editor shell). A group id the
        # worker never owned is never reaped, only reported.
        return ()
    members: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        fields = _stat_fields(pid)
        if not fields or fields[0] in _DEAD_STAT_STATES:
            continue
        if _stat_process_group(fields) == pgid:
            members.append(pid)
    return tuple(members)


def _stat_fields(pid: int) -> list[str] | None:
    """``/proc/<pid>/stat`` split after the executable name, or ``None``.

    Parsed from the closing parenthesis because the second field is the
    executable name in parentheses and may itself contain spaces and
    parentheses. What comes back is 0-based from ``state``, so index 0 is the
    process state and index 2 is the process group id.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, PermissionError, ProcessLookupError):
        return None
    end = stat.rfind(")")
    if end == -1:
        return None
    return stat[end + 2 :].split()


def _stat_process_group(fields: Sequence[str]) -> int | None:
    """Field 5 of ``/proc/<pid>/stat``, which is the process group id."""
    if len(fields) < 3:
        return None
    try:
        return int(fields[2])
    except ValueError:  # pragma: no cover - the kernel does not write this
        return None


# ---------------------------------------------------------------------------
# the chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoSeedOptions:
    """Operator choices for one ``seed:demo`` run.

    The two embedding values are required and have no default: the model and
    dimension are one fixed value on both sides, and guessing one would make the
    embed probe a check against a value this seed itself invented.
    """

    expected_dimensions: int
    expected_generation: str
    fixture_dir: Path | str = DEMO_FIXTURE_DIR
    #: ``--reset``: wipe the allowlisted tables, the ES indices, the graph and
    #: the catalog demo rows.
    reset: bool = False
    #: ``--yes``: perform the writes. Without it the run is a preview.
    write: bool = False
    catalog_path: Path | str | None = None
    user_home: Path | str | None = None
    poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS


@dataclass
class DemoSeedDeps:
    """Every collaborator the chain uses, injected.

    Nothing here is constructed by default: the CLI is the composition root,
    and a test drives the same chain with fakes and no dev stack.
    """

    environ: Mapping[str, str]
    push_port: PushPort
    index_job_port: IndexJobPort
    reset_port: DevStackResetPort
    plan_a: PlanAEntryPoints
    embed_probe: EmbedProbePort
    worker_factory: Callable[[], WorkerHandle]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep


@dataclass(frozen=True)
class JobOutcome:
    """One unit's job as the run left it."""

    document_id: str
    unit: int
    job_id: str
    status: str
    reused: bool
    skipped: bool
    failure_code: str | None


@dataclass(frozen=True)
class DemoSeedReport:
    """What the run did -- the preview and the ``--yes`` run share it."""

    documents: tuple[DemoDocument, ...]
    wrote: bool
    reset_performed: bool
    jobs: tuple[JobOutcome, ...]
    #: The source refs the service had never seen, so this run pushed them.
    ingested: tuple[str, ...]
    catalog_rows_written: int
    catalog_path: Path | None
    elapsed_seconds: float
    reset_outcome: ResetOutcome | None = None


@dataclass
class _PendingJob:
    """A job the run submitted or joined and still has to settle."""

    unit: DemoUnit
    job_id: str
    reused: bool
    skipped: bool = False
    status: str = _PENDING_STATUS


def run_demo_seed(
    deps: DemoSeedDeps,
    options: DemoSeedOptions,
) -> DemoSeedReport:
    """Run the chain and return its report.

    Order is the spec's: guard, ``--reset``, schema, embed probe, preview,
    and only under ``--yes`` the submit / worker / poll writes. A preview that
    returned before the writes leaves every store untouched, which is why the
    submit step sits behind :attr:`DemoSeedOptions.write`.
    """
    started = deps.clock()
    documents = load_demo_documents(options.fixture_dir)
    guard_dev_stack(deps.environ, user_home=options.user_home)
    # ``--reset`` is a write, so it is gated by ``--yes`` exactly like the
    # submit is (ADR 0100 decision 4: write only with ``--yes``). A preview
    # that truncated tables would be the most destructive thing the command
    # could do without anyone having said yes.
    reset_outcome = (
        reset_dev_stack(
            reset_port=deps.reset_port,
            plan_a=deps.plan_a,
            catalog_path=options.catalog_path,
            document_ids=[document.document_id for document in documents],
        )
        if options.reset and options.write
        else None
    )
    deps.plan_a.apply_schema()
    probe_embedding_generation(
        deps.embed_probe,
        expected_dimensions=options.expected_dimensions,
        expected_generation=options.expected_generation,
    )
    _preview(documents, options)
    if not options.write:
        if options.reset:
            print(
                "[seed:demo] --reset requested but not performed: a preview "
                "writes nothing; re-run with --yes to reset and seed"
            )
        return _preview_report(documents, options, reset_outcome, started, deps)
    pending, ingested = _submit_all(documents, deps)
    ready = tuple(job for job in pending if not job.skipped)
    ready_outcomes = iter(_await_jobs(ready, deps, options) if ready else ())
    outcomes = [
        _outcome_of(job, status=_READY_STATUS)
        if job.skipped
        else next(ready_outcomes)
        for job in pending
    ]
    catalog_rows = seed_demo_catalog(
        options.catalog_path, fixture_dir=options.fixture_dir
    )[0]
    return DemoSeedReport(
        documents=documents,
        wrote=True,
        reset_performed=options.reset,
        jobs=tuple(outcomes),
        ingested=ingested,
        catalog_rows_written=catalog_rows,
        catalog_path=Path(options.catalog_path)
        if options.catalog_path is not None
        else None,
        elapsed_seconds=deps.clock() - started,
        reset_outcome=reset_outcome,
    )


def _preview(
    documents: Sequence[DemoDocument], options: DemoSeedOptions
) -> None:
    """Print what a ``--yes`` run would do. Print-only, by construction."""
    mode = "write" if options.write else "preview"
    print(f"[seed:demo] {mode}: {len(documents)} documents from {options.fixture_dir}")
    for document in documents:
        print(
            f"[seed:demo]   {document.document_id} type={document.document_type} "
            f"pages={document.page_count} units={len(document.units)}"
        )


def _preview_report(
    documents: Sequence[DemoDocument],
    options: DemoSeedOptions,
    reset_outcome: ResetOutcome | None,
    started: float,
    deps: DemoSeedDeps,
) -> DemoSeedReport:
    """The preview's report: the jobs it would create, and nothing written."""
    return DemoSeedReport(
        documents=tuple(documents),
        wrote=False,
        reset_performed=options.reset,
        jobs=tuple(
            JobOutcome(
                document_id=unit.document.document_id,
                unit=unit.unit,
                job_id="",
                status=_PENDING_STATUS,
                reused=False,
                skipped=False,
                failure_code=None,
            )
            for document in documents
            for unit in document.units
        ),
        ingested=(),
        catalog_rows_written=0,
        catalog_path=Path(options.catalog_path)
        if options.catalog_path is not None
        else None,
        elapsed_seconds=deps.clock() - started,
        reset_outcome=reset_outcome,
    )


def _submit_all(
    documents: Sequence[DemoDocument],
    deps: DemoSeedDeps,
) -> tuple[tuple[_PendingJob, ...], tuple[str, ...]]:
    """Submit every unit, ingesting first when the stack has no such source.

    Submit comes before ingest because it is a pure read of the durable pair:
    a pair the service already holds is joined instead of pushed again, which is
    what makes a second ``--yes`` run a no-op. The one case that has to write
    first is a source the service has never seen, and it is named
    (``index_job_unknown_source``) rather than inferred.

    The unit is the granularity on both sides: one source ref, one envelope and
    one job per unit, so a three-page document costs three jobs and a hit on
    its second page names that page.
    """
    pending: list[_PendingJob] = []
    ingested: list[str] = []
    for document in documents:
        for unit in document.units:
            identity = unit.content_identity
            try:
                submission = deps.index_job_port.submit_index_job(
                    unit.source_ref, identity
                )
            except IndexJobUnknownSourceError:
                deps.push_port.push([unit.push_envelope()])
                ingested.append(unit.source_ref)
                submission = deps.index_job_port.submit_index_job(
                    unit.source_ref, identity
                )
            record = submission.record
            job = _PendingJob(
                unit=unit,
                job_id=record.job_id,
                reused=submission.reused,
            )
            if submission.reused and record.status in _TERMINAL_FAILURE_STATUSES:
                raise ReusedFailedJobError(
                    _job_failure_message(
                        record,
                        deps=deps,
                        prefix=(
                            f"submit joined an already {record.status} job for "
                            f"{unit.source_ref!r}"
                        ),
                    ),
                    job_id=record.job_id,
                    failure_code=record.failure_code,
                    reset_command=deps.plan_a.reset_job_command(record.job_id),
                )
            job.status = record.status
            job.skipped = submission.reused and record.status == _READY_STATUS
            pending.append(job)
    return tuple(pending), tuple(ingested)


def _await_jobs(
    pending: Sequence[_PendingJob],
    deps: DemoSeedDeps,
    options: DemoSeedOptions,
) -> tuple[JobOutcome, ...]:
    """Run the worker and poll every open job to a terminal state.

    The guard runs again here, immediately before the worker starts: the window
    between the first guard and this subprocess is exactly where a re-pointed
    stack would slip through.

    ``open_ids`` is handed to the poll so the teardown can report how many jobs
    were still open when the worker was ended, and it is read after the
    ``finally`` because the count that matters is the one at teardown time --
    a poll that raised part-way through still leaves the rest open.
    """
    memory_home = guard_dev_stack(deps.environ, user_home=options.user_home)
    worker = deps.worker_factory()
    open_ids: list[str] = []
    statuses: dict[str, IndexJobRecord] = {}
    try:
        worker.start(memory_home=memory_home)
        statuses = _poll_until_terminal(
            pending, deps=deps, options=options, open_ids=open_ids
        )
    finally:
        worker.terminate_and_wait(
            timeout_seconds=WORKER_TERMINATE_TIMEOUT_SECONDS,
            open_jobs=len(open_ids),
        )
    return tuple(
        _outcome_of(
            job,
            status=(
                statuses[job.job_id].status
                if job.job_id in statuses
                else job.status
            ),
        )
        for job in pending
    )


def _poll_until_terminal(
    pending: Sequence[_PendingJob],
    *,
    deps: DemoSeedDeps,
    options: DemoSeedOptions,
    open_ids: list[str],
) -> dict[str, IndexJobRecord]:
    """Poll the open jobs until each reaches ``ready`` or a terminal failure.

    ``open_ids`` is the caller's view of what is still open: it is the same
    list this function drains, so the count the teardown reports is the count
    the poll was working on, not a recount that could disagree with it.
    """
    open_ids.extend(job.job_id for job in pending)
    latest: dict[str, IndexJobRecord] = {}
    deadline = deps.clock() + options.poll_timeout_seconds
    while open_ids:
        for job_id in list(open_ids):
            record = deps.index_job_port.poll_index_job(job_id).record
            latest[job_id] = record
            if record.status == _READY_STATUS:
                open_ids.remove(job_id)
            elif record.status in _TERMINAL_FAILURE_STATUSES:
                raise _job_failure_error(record, deps=deps)
        if not open_ids:
            break
        if deps.clock() >= deadline:
            raise DemoPollTimeoutError(
                f"the poll budget of {options.poll_timeout_seconds:g}s expired "
                f"with {len(open_ids)} job(s) still open: "
                f"{', '.join(open_ids)}; reset a stuck job with: "
                f"{' '.join(deps.plan_a.reset_job_command(open_ids[0]))}",
                job_ids=tuple(open_ids),
                reset_command=deps.plan_a.reset_job_command(open_ids[0]),
            )
        deps.sleep(options.poll_interval_seconds)
    return latest


def _outcome_of(job: _PendingJob, *, status: str) -> JobOutcome:
    return JobOutcome(
        document_id=job.unit.document.document_id,
        unit=job.unit.unit,
        job_id=job.job_id,
        status=status,
        reused=job.reused,
        skipped=job.skipped,
        failure_code=None if status == _READY_STATUS else status,
    )


def _job_failure_error(record: IndexJobRecord, *, deps: DemoSeedDeps) -> DemoJobFailedError:
    """The typed failure a non-ready terminal state raises."""
    reset_command = deps.plan_a.reset_job_command(record.job_id)
    error_type: type[DemoJobFailedError] = (
        EmbedLimitBreachedError
        if record.failure_code == ENTITY_MERGE_EMBED_LIMIT
        else DemoJobFailedError
    )
    return error_type(
        _job_failure_message(record, deps=deps),
        job_id=record.job_id,
        failure_code=record.failure_code,
        reset_command=reset_command,
    )


def _job_failure_message(
    record: IndexJobRecord, *, deps: DemoSeedDeps, prefix: str = ""
) -> str:
    """One message naming the job, the failure code and the reset command."""
    head = prefix or f"job {record.job_id!r} for {record.source_ref!r} ended {record.status}"
    return (
        f"{head}; job_id={record.job_id}; "
        f"failure_code={record.failure_code or record.status}; "
        f"reset with: {' '.join(deps.plan_a.reset_job_command(record.job_id))}"
    )


def exit_code_for(error: BaseException) -> int:
    """The process exit code one failure maps to.

    Guard refusals -- including the embedding generation mismatch -- exit 2, as
    the acceptance requires; every other named failure exits 1. The mapping is a
    property of the exception class, so it cannot drift with print order.
    """
    if isinstance(error, DemoSeedError):
        return error.exit_code
    return 1


__all__ = [
    "CONTENT_IDENTITY_VERSIONS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_POLL_TIMEOUT_SECONDS",
    "DEMO_FIXTURE_DIR",
    "DEV_STACK_PORTS",
    "DemoDefaultMemoryHomeError",
    "DemoDocument",
    "DemoEmbeddingMismatchError",
    "DemoFixtureError",
    "DemoFixtureMissingError",
    "DemoJobFailedError",
    "DemoMemoryHomeUnsetError",
    "DemoNeo4jUnavailableError",
    "DemoNonPersistentStorageError",
    "DemoPollTimeoutError",
    "DemoPortMismatchError",
    "DemoSeedDeps",
    "DemoSeedError",
    "DemoSeedGuardError",
    "DemoSeedOptions",
    "DemoSeedReport",
    "DemoStoreUnavailableError",
    "DemoUnit",
    "DemoWorkerError",
    "DemoWorkerUnavailableError",
    "DevStackResetPort",
    "ElasticsearchIndexReset",
    "EmbedGeneration",
    "EmbedLimitBreachedError",
    "EmbedProbePort",
    "JobOutcome",
    "Neo4jGraphReset",
    "PLAN_A_NPM_SCRIPTS",
    "PLAN_A_TABLE_ALLOWLIST_FILE",
    "PlanADevStackAdapter",
    "PlanAEntryPointError",
    "PlanAEntryPoints",
    "PostgresEmbedProbe",
    "PostgresStoreReset",
    "ResetOutcome",
    "ReusedFailedJobError",
    "SEED_RESULT_FAILED",
    "SEED_RESULT_PREFIX",
    "SEED_RESULT_REFUSED",
    "SubprocessWorker",
    "WorkerHandle",
    "clear_demo_catalog_rows",
    "content_identity_of",
    "demo_catalog_rows",
    "exit_code_for",
    "format_seed_result",
    "guard_dev_stack",
    "load_demo_documents",
    "parse_seed_result",
    "probe_embedding_generation",
    "reset_dev_stack",
    "run_demo_seed",
    "seed_demo_catalog",
    "worker_command",
]
