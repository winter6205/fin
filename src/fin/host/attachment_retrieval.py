"""Thread-scoped attachment search (G4 T3).

The attachment branch of the host ``retrieve`` tool. Selection follows the
spec's source-selection rule: ready attachments in the ACTIVE thread are
eligible by default, naming an attachment narrows only this branch, and a
thread scope the model cannot widen — the caller supplies ``thread_id`` from
the host-bound run config, never from a tool argument (ADR 0088).

This module owns the contract shapes only the host consumes:
- :class:`AttachmentLocator` — the typed identity carried as
  ``source_ref={kind:"attachment",attachment_id,version,unit}`` on a hit
  (ADR 0088; ``unit`` is the one-based frozen-unit ordinal).
- :class:`AttachmentChunk` — one located unit of parsed text with a score.
- the typed branch failures: an unknown/cross-thread named identity shares
  ``unknown_locator`` behavior and reveals nothing; a ``ready`` row whose
  postings are missing or unusable is ``attachment_index_unavailable``,
  never an empty successful hit set.
- the failed-row exit: a named ``failed`` row re-publishes its STORED typed
  failure code (never collapsed into ``attachment_not_ready``); a row whose
  stored reason is empty falls back to ``attachment_preparation_failed``.

Company ranking stays behind the existing retrieve port; nothing here
imports or duplicates it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar, Literal, Protocol, Sequence

from fin.host import attachment_index
from fin.host.attachment_index import is_usable
from fin.host.attachment_store import AttachmentRecord, ParsedAttachmentUnit
from fin.host.degradation_notice import ATTACHMENT_PREPARATION_FAILED

ATTACHMENT_SOURCE_KIND: Literal["attachment"] = "attachment"


@dataclass(frozen=True)
class AttachmentLocator:
    """One frozen unit of one attachment version, owned by one thread.

    ``unit`` is the one-based ordinal the index ranks; it is the value the
    hit publishes as ``source_ref.unit``. The owning ``thread_id`` is NOT
    part of the locator: scope is host-resolved at search time, so a
    locator can never smuggle a thread override.
    """

    attachment_id: str
    version: int
    unit: int
    kind: Literal["attachment"] = ATTACHMENT_SOURCE_KIND

    def as_source_ref(self) -> dict[str, object]:
        """The JSON shape serialized onto a retrieved chunk."""
        return {
            "kind": self.kind,
            "attachment_id": self.attachment_id,
            "version": self.version,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class AttachmentChunk:
    """One attachment hit: unit text plus its typed locator and score."""

    text: str
    source_ref: AttachmentLocator
    score: float


class AttachmentRetrievalError(Exception):
    """Typed failure of the attachment search branch. Carries a wire code.

    ``code`` resolves instance-else-class-default: an instance built with
    the ``code`` kwarg answers with that named code (how a ``failed`` row
    re-publishes its stored failure reason), and every other instance
    answers with its class's ``_DEFAULT_CODE``. A blank or whitespace-only
    override is rejected because the graph envelope publishes the code
    verbatim — an empty one would be an untyped ``{"error": ""}``.
    """

    _DEFAULT_CODE: ClassVar[str] = "attachment_retrieval_failed"

    def __init__(
        self,
        *,
        attachment_id: str = "",
        detail: str = "",
        code: str | None = None,
    ) -> None:
        if code is not None and not code.strip():
            raise ValueError(
                "the contract publishes ``code`` verbatim, so it must be a "
                "non-blank wire code"
            )
        self.code: str = code or self._DEFAULT_CODE
        super().__init__(detail or self.code)
        self.attachment_id = attachment_id
        self.detail = detail

    def as_dict(self) -> dict[str, str]:
        return {"error": self.code}


class AttachmentUnknownLocatorError(AttachmentRetrievalError):
    """Named identity not owned by the active thread (or absent/deleted).

    Shares the ``unknown_locator`` wire code with the read doors so
    cross-thread probing cannot distinguish "exists elsewhere" from
    "never existed".
    """

    _DEFAULT_CODE: ClassVar[str] = "unknown_locator"


class AttachmentNotReadyError(AttachmentRetrievalError):
    """A named identity exists in-thread but is not searchable yet.

    Residual scope is the gate-owned transient states (``uploaded``,
    ``parsing``): a run that made such a row eligible suspends in
    ``ensure_attachment_ready`` before retrieve runs, so answering
    ``attachment_not_ready`` here is the search-level race for a row
    still in flight — a state a later call can leave ready. A terminal
    ``failed`` row is NOT this code: it exits with its stored named
    failure (spec: a parse failure is never retried as a wait).
    """

    _DEFAULT_CODE: ClassVar[str] = "attachment_not_ready"


class AttachmentIndexUnavailableError(AttachmentRetrievalError):
    """A ``ready`` attachment's postings are missing or fail the usability
    gate. Typed unavailability, never an empty successful hit set."""

    _DEFAULT_CODE: ClassVar[str] = "attachment_index_unavailable"


class AttachmentSearchPort(Protocol):
    """Consumer-side seam the host retrieve tool calls.

    Implementations must scope every candidate to ``thread_id`` and raise
    the typed errors above; they never raise for a legal query that simply
    has no hits (that is an empty sequence). ``limit=None`` means the
    implementation's default (8).
    """

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> Sequence[AttachmentChunk]: ...


class AttachmentSearchStore(Protocol):
    """The narrow store seam the retriever consumes.

    Four read methods, shaped exactly as :class:`fin.host.attachment_store.
    AttachmentStore` declares them — the retriever writes nothing and asks
    nothing about lifecycle, so the search side cannot disturb preparation.
    """

    def get(self, attachment_id: str) -> AttachmentRecord | None: ...

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]: ...

    def units(
        self, attachment_id: str, version: int
    ) -> tuple[ParsedAttachmentUnit, ...]: ...

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]: ...


class ThreadAttachmentRetriever:
    """Implements :class:`AttachmentSearchPort` over one thread's rows.

    Every exit is typed: identity mistakes and unreadable indexes raise,
    and only a query that legitimately matched nothing answers empty.
    """

    _DEFAULT_LIMIT: ClassVar[int] = 8
    _MAX_LIMIT: ClassVar[int] = 20

    def __init__(self, store: AttachmentSearchStore) -> None:
        self._store = store

    def search(
        self,
        *,
        thread_id: str,
        query: str,
        attachment_ids: Sequence[str] | None = None,
        limit: int | None = None,
    ) -> tuple[AttachmentChunk, ...]:
        """Search this thread's ready attachments; ``limit`` defaults to 8."""
        if not query.strip():
            # EXIT: a blank query selected nothing; it is the one legal
            # empty and must not be turned into a typed failure even when
            # the thread's index happens to be broken.
            return ()

        candidates = self._candidates(thread_id=thread_id, attachment_ids=attachment_ids)
        chunks: list[AttachmentChunk] = []
        for record in candidates:
            postings = self._store.index_postings(
                record.attachment_id, record.version
            )
            if not is_usable(postings):
                # EXIT: the row claims ready but its postings do not back
                # that claim — the branch is unavailable, never an empty
                # success that would read as "the text is not in there".
                raise AttachmentIndexUnavailableError(
                    attachment_id=record.attachment_id,
                    detail=(
                        f"version {record.version} of {record.attachment_id!r} "
                        "has no usable derived index"
                    ),
                )
            for hit in attachment_index.search(postings, query):
                chunks.append(self._chunk(record, hit))

        chunks.sort(
            key=lambda chunk: (
                -chunk.score,
                chunk.source_ref.unit,
                chunk.source_ref.attachment_id,
            )
        )
        return tuple(chunks[: self._clamp_limit(limit)])

    def _candidates(
        self,
        *,
        thread_id: str,
        attachment_ids: Sequence[str] | None,
    ) -> list[AttachmentRecord]:
        if attachment_ids is None:
            # WHY re-check thread_id though list_for_thread already scopes:
            # scope is this branch's whole safety property, so the
            # retriever does not delegate it to one store's correctness.
            return [
                record
                for record in self._store.list_for_thread(thread_id)
                if record.thread_id == thread_id and record.searchable
            ]
        wanted: list[AttachmentRecord] = []
        seen: set[str] = set()
        for name in attachment_ids:
            if name in seen:
                continue
            seen.add(name)
            record = self._store.get(name)
            if record is None or record.thread_id != thread_id:
                # EXIT: unknown, other-thread, and tombstoned share one
                # reveal-nothing exit (see AttachmentUnknownLocatorError).
                raise AttachmentUnknownLocatorError(
                    attachment_id=name,
                    detail=f"{name!r} is not an attachment of this thread",
                )
            if record.status == "failed":
                # EXIT: a named failed row re-publishes its stored reason
                # as the code (spec: named failure) — collapsing it into
                # attachment_not_ready would invite the model to
                # wait-and-retry a terminal failure. The fallback condition
                # is a ``failed`` row whose stored failure_reason is empty
                # — a shape the store's mark_failed door refuses to write —
                # so the exit still names the shared wire code rather than
                # an empty ``{"error": ""}`` envelope.
                reason = record.failure_reason or ATTACHMENT_PREPARATION_FAILED
                raise AttachmentRetrievalError(
                    attachment_id=name,
                    code=reason,
                    detail=f"{name!r} failed preparation ({reason})",
                )
            if not record.searchable:
                raise AttachmentNotReadyError(
                    attachment_id=name,
                    detail=(
                        f"{name!r} is {record.status}, not searchable yet"
                    ),
                )
            wanted.append(record)
        return wanted

    def _chunk(
        self, record: AttachmentRecord, hit: attachment_index.IndexHit
    ) -> AttachmentChunk:
        rows = self._store.units(record.attachment_id, record.version)
        index = hit.unit - 1
        # WHY the ordinal is checked against the row it addresses, not just
        # the bounds: a posting may not attribute returned text to a unit
        # the frozen rows never carried — that is the citation error no
        # downstream gate can catch.
        if index < 0 or index >= len(rows) or rows[index].unit != hit.unit:
            raise AttachmentIndexUnavailableError(
                attachment_id=record.attachment_id,
                detail=(
                    f"posting for unit {hit.unit} of version "
                    f"{record.version} addresses no frozen unit row"
                ),
            )
        return AttachmentChunk(
            text=rows[index].text,
            source_ref=AttachmentLocator(
                attachment_id=record.attachment_id,
                version=record.version,
                unit=hit.unit,
            ),
            score=float(len(hit.matched)),
        )

    @classmethod
    def _clamp_limit(cls, limit: int | None) -> int:
        # WHY None lands here too: ``_DEFAULT_LIMIT`` is the single source
        # for the default window; no attachment-branch caller repeats the
        # literal — the graph tool's model-facing default is the wire
        # default and is forwarded verbatim.
        if limit is None or limit <= 0:
            return cls._DEFAULT_LIMIT
        return min(limit, cls._MAX_LIMIT)


__all__ = [
    "ATTACHMENT_SOURCE_KIND",
    "AttachmentChunk",
    "AttachmentIndexUnavailableError",
    "AttachmentLocator",
    "AttachmentNotReadyError",
    "AttachmentRetrievalError",
    "AttachmentSearchPort",
    "AttachmentSearchStore",
    "AttachmentUnknownLocatorError",
    "ThreadAttachmentRetriever",
]
