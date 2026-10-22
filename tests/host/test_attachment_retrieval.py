"""``fin.host.attachment.attachment_retrieval.ThreadAttachmentRetriever`` (G4 T3).

Behavior pinned from specs/thread-attachment-source-read.md (source-selection
and typed-exit items) and plans/thread-attachment-source-read.md item 8:

* candidates are ready+searchable attachments of the ACTIVE thread only;
  a model-supplied thread id can never widen scope (the caller passes it,
  and the retriever re-checks ownership per row as defense in depth);
* naming attachments narrows this branch; a name the thread does not own
  (unknown, other-thread, tombstoned) shares one reveal-nothing
  ``unknown_locator`` exit; a name owned but still in a gate-owned
  transient state (``uploaded``, ``parsing``) exits
  ``attachment_not_ready``; a ``failed`` name exits with its stored
  named failure code (spec line 61: a parse failure is never collapsed
  into a generic not-ready).
* a ``ready`` row whose postings fail the usability gate, or whose postings
  address a unit ordinal the frozen rows do not carry, is a typed
  ``attachment_index_unavailable`` for the whole branch — never an empty
  success;
* ranking is :func:`fin.host.attachment.attachment_index.search` per candidate, merged
  with the deterministic key (score desc, unit asc) and clamped limit.

Seeding goes through ``InMemoryAttachmentStore``'s public doors
(create → record_units → put_index → mark_ready); the two corrupt shapes no
public door can produce ride on thin delegating wrappers, following the
``_force_parsing`` precedent in test_attachment_store.py (pin the stored
shape, not a door).
"""

from __future__ import annotations

import dataclasses
import uuid

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment.attachment_index import build_postings
from fin.host.attachment.attachment_prepare import AttachmentPreparationService
from fin.host.attachment.attachment_retrieval import (
    AttachmentIndexUnavailableError,
    AttachmentLocator,
    AttachmentNotReadyError,
    AttachmentRetrievalError,
    AttachmentUnknownLocatorError,
    ThreadAttachmentRetriever,
)
from fin.host.attachment.attachment_store import (
    AttachmentRecord,
    InMemoryAttachmentStore,
    ParsedAttachmentUnit,
)
from fin.host.thread_store import InMemoryThreadStore
from attachment_fakes import DelegatingStore

UNITS = (
    ParsedAttachmentUnit(unit=1, text="Q3 revenue was 89,493 million dollars."),
    ParsedAttachmentUnit(unit=2, text="营业收入 89,493 百万美元。"),
)


def _boot() -> tuple[InMemoryAttachmentStore, str, str]:
    """A live thread and its attachment store."""
    threads = InMemoryThreadStore()
    store = InMemoryAttachmentStore(threads)
    other = threads.create_thread(title="Other thread")
    own = threads.create_thread(title="Revenue thread")
    return store, own.id, other.id


def _seed_ready(
    store: InMemoryAttachmentStore,
    thread_id: str,
    *,
    aid: str | None = None,
    units: tuple[ParsedAttachmentUnit, ...] = UNITS,
    version: int = 1,
) -> AttachmentRecord:
    """A row published ``ready`` through the store's own doors."""
    aid = aid or f"att-{uuid.uuid4().hex[:8]}"
    store.create(
        thread_id=thread_id,
        attachment_id=aid,
        filename="notes.txt",
        size_bytes=1_024,
        sha256="ab" * 32,
        object_key=f"thread-attachments/{thread_id}/{aid}",
    )
    store.record_units(attachment_id=aid, version=version, units=units)
    store.put_index(
        attachment_id=aid, version=version, postings=build_postings(units)
    )
    return store.mark_ready(
        attachment_id=aid, version=version, unit_count=len(units)
    )


def _seed_uploaded(
    store: InMemoryAttachmentStore, thread_id: str, *, aid: str | None = None
) -> AttachmentRecord:
    aid = aid or f"att-{uuid.uuid4().hex[:8]}"
    return store.create(
        thread_id=thread_id,
        attachment_id=aid,
        filename="draft.txt",
        size_bytes=64,
        sha256="cd" * 32,
        object_key=f"thread-attachments/{thread_id}/{aid}",
    )


class _UnusableIndexStore(DelegatingStore):
    """Delegates everything but answers a ready row's postings as missing.

    WHY a wrapper: every public door of the store refuses to leave a ready
    attachment without usable postings, so the torn-rebuild shape the
    retriever must type can only arrive as a stored state, not as a door
    outcome.
    """

    def index_postings(
        self, attachment_id: str, version: int
    ) -> dict[str, tuple[int, ...]]:
        return {}


class _ReasonlessFailedStore(DelegatingStore):
    """Delegates but blanks a failed row's stored reason.

    WHY a wrapper: the store's ``mark_failed`` door rejects a blank
    reason, so the broken-row shape the fallback must survive can only
    arrive as a stored state, not as a door outcome (same precedent as
    :class:`_UnusableIndexStore`).
    """

    def get(self, attachment_id: str) -> AttachmentRecord | None:
        record = self._inner.get(attachment_id)
        if record is not None and record.status == "failed":
            return dataclasses.replace(record, failure_reason="")
        return record


class _LeakyListingStore(DelegatingStore):
    """Delegates but lists another thread's rows into this thread.

    WHY a wrapper: pins the retriever's own ownership filter, the defense
    in depth that must hold even if a store release ever leaks rows.
    """

    def __init__(self, inner: InMemoryAttachmentStore, extra_ids: tuple[str, ...]):
        super().__init__(inner)
        self._extra_ids = extra_ids

    def list_for_thread(self, thread_id: str) -> list[AttachmentRecord]:
        leaked = [
            record
            for record in (self._inner.get(a) for a in self._extra_ids)
            if record is not None
        ]
        return self._inner.list_for_thread(thread_id) + leaked


# ----- default candidates and locator shape -----


def test_hit_carries_typed_locator_and_unit_text() -> None:
    store, own, _ = _boot()
    record = _seed_ready(store, own)
    chunks = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="revenue million"
    )
    assert len(chunks) == 1
    chunk = chunks[0]
    assert chunk.source_ref == AttachmentLocator(
        attachment_id=record.attachment_id, version=1, unit=1
    )
    assert chunk.text == UNITS[0].text
    assert chunk.score == 2.0


def test_locator_carries_the_records_published_version() -> None:
    store, own, _ = _boot()
    record = _seed_ready(store, own, version=2)
    (chunk,) = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="revenue"
    )
    assert record.version == 2
    assert chunk.source_ref.version == 2
    assert chunk.source_ref.as_source_ref() == {
        "kind": "attachment",
        "attachment_id": record.attachment_id,
        "version": 2,
        "unit": 1,
    }


def test_cjk_query_hits_the_cjk_unit() -> None:
    store, own, _ = _boot()
    record = _seed_ready(store, own)
    (chunk,) = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="营业收入"
    )
    assert chunk.source_ref.unit == 2
    assert chunk.text == UNITS[1].text
    assert chunk.source_ref.attachment_id == record.attachment_id


def test_not_searchable_rows_are_not_candidates() -> None:
    store, own, _ = _boot()
    _seed_uploaded(store, own)
    ready = _seed_ready(store, own)
    store.mark_parsing(attachment_id=ready.attachment_id, from_statuses=("ready",))
    chunks = ThreadAttachmentRetriever(store).search(thread_id=own, query="revenue")
    assert chunks == ()


# ----- thread scope -----


def test_other_threads_attachments_never_appear() -> None:
    store, own, other = _boot()
    _seed_ready(store, other)
    assert ThreadAttachmentRetriever(store).search(
        thread_id=own, query="revenue"
    ) == ()


def test_listing_leak_is_still_filtered_by_ownership() -> None:
    store, own, other = _boot()
    foreign = _seed_ready(store, other)
    own_record = _seed_ready(store, own)
    chunks = ThreadAttachmentRetriever(_LeakyListingStore(store, (foreign.attachment_id,))).search(
        thread_id=own, query="revenue"
    )
    assert [c.source_ref.attachment_id for c in chunks] == [own_record.attachment_id]


# ----- naming -----


def test_naming_narrows_to_the_named_attachment() -> None:
    store, own, _ = _boot()
    first = _seed_ready(store, own, units=(ParsedAttachmentUnit(unit=1, text="alpha report"),))
    second = _seed_ready(store, own, units=(ParsedAttachmentUnit(unit=1, text="alpha memo"),))
    both = ThreadAttachmentRetriever(store).search(thread_id=own, query="alpha")
    assert {c.source_ref.attachment_id for c in both} == {
        first.attachment_id,
        second.attachment_id,
    }
    chunks = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="alpha", attachment_ids=[second.attachment_id]
    )
    assert {c.source_ref.attachment_id for c in chunks} == {second.attachment_id}


@pytest.mark.parametrize("shape", ["unknown", "other-thread", "tombstoned"])
def test_named_identity_the_thread_does_not_own_exits_unknown_locator(
    shape: str,
) -> None:
    store, own, other = _boot()
    if shape == "unknown":
        named = "att-never-existed"
    elif shape == "other-thread":
        named = _seed_ready(store, other).attachment_id
    else:
        record = _seed_ready(store, own)
        named = record.attachment_id
        store.tombstone(attachment_id=named, thread_id=own)
    with pytest.raises(AttachmentUnknownLocatorError) as excinfo:
        ThreadAttachmentRetriever(store).search(
            thread_id=own, query="revenue", attachment_ids=[named]
        )
    assert excinfo.value.code == "unknown_locator"


@pytest.mark.parametrize("door", ["uploaded", "parsing"])
def test_named_owned_but_not_searchable_exits_not_ready(door: str) -> None:
    store, own, _ = _boot()
    record = _seed_uploaded(store, own)
    if door == "parsing":
        store.mark_parsing(attachment_id=record.attachment_id)
    with pytest.raises(AttachmentNotReadyError) as excinfo:
        ThreadAttachmentRetriever(store).search(
            thread_id=own, query="revenue", attachment_ids=[record.attachment_id]
        )
    assert excinfo.value.code == "attachment_not_ready"


def test_named_failed_row_exits_with_its_stored_failure_code() -> None:
    """Spec line 61: "Parse failure exits with its named typed failure".

    Seeded through the store's public failure doors (mark_parsing →
    mark_failed, the same path test_attachment_prepare_door.py asserts
    the stored ``failure_reason`` on), so the pinned exit rides the real
    durable shape.
    """
    store, own, _ = _boot()
    record = _seed_uploaded(store, own)
    store.mark_parsing(attachment_id=record.attachment_id)
    store.mark_failed(
        attachment_id=record.attachment_id,
        version=0,
        reason="attachment_empty_text",
    )
    with pytest.raises(AttachmentRetrievalError) as excinfo:
        ThreadAttachmentRetriever(store).search(
            thread_id=own, query="revenue", attachment_ids=[record.attachment_id]
        )
    # The named failure, never the generic wait-and-retry code.
    assert excinfo.value.code == "attachment_empty_text"
    assert excinfo.value.code != "attachment_not_ready"


def test_failed_row_without_a_stored_reason_still_names_a_code() -> None:
    """The defense-in-depth fallback: a broken row (status ``failed``,
    blank reason) exits typed, never as not-ready and never with an
    empty code."""
    store, own, _ = _boot()
    record = _seed_uploaded(store, own)
    store.mark_parsing(attachment_id=record.attachment_id)
    store.mark_failed(
        attachment_id=record.attachment_id, version=0, reason="attachment_empty_text"
    )
    with pytest.raises(AttachmentRetrievalError) as excinfo:
        ThreadAttachmentRetriever(_ReasonlessFailedStore(store)).search(
            thread_id=own, query="revenue", attachment_ids=[record.attachment_id]
        )
    assert excinfo.value.code == "attachment_preparation_failed"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_blank_code_override_is_rejected(blank: str) -> None:
    """The graph envelope publishes ``code`` verbatim, so a blank override
    would mint the untyped ``{"error": ""}`` the contract forbids."""
    with pytest.raises(ValueError):
        AttachmentRetrievalError(code=blank)


def test_code_resolves_override_else_class_default() -> None:
    """A legal override rides the instance; every no-override instance
    answers with its own class default (no shadowing required)."""
    assert AttachmentRetrievalError(code="attachment_empty_text").code == (
        "attachment_empty_text"
    )
    assert AttachmentRetrievalError().code == "attachment_retrieval_failed"
    assert AttachmentUnknownLocatorError().code == "unknown_locator"
    assert AttachmentNotReadyError().code == "attachment_not_ready"
    assert AttachmentIndexUnavailableError().code == "attachment_index_unavailable"
    # ``as_dict`` keeps publishing the instance code.
    assert AttachmentRetrievalError(code="attachment_empty_text").as_dict() == {
        "error": "attachment_empty_text"
    }


def test_empty_naming_narrows_to_nothing() -> None:
    store, own, _ = _boot()
    _seed_ready(store, own)
    chunks = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="revenue", attachment_ids=[]
    )
    assert chunks == ()


# ----- typed index unavailability -----


def test_ready_row_with_unusable_postings_fails_the_branch() -> None:
    store, own, _ = _boot()
    _seed_ready(store, own)
    with pytest.raises(AttachmentIndexUnavailableError) as excinfo:
        ThreadAttachmentRetriever(_UnusableIndexStore(store)).search(
            thread_id=own, query="revenue"
        )
    assert excinfo.value.code == "attachment_index_unavailable"


def test_named_ready_row_with_unusable_postings_fails_the_branch() -> None:
    store, own, _ = _boot()
    record = _seed_ready(store, own)
    with pytest.raises(AttachmentIndexUnavailableError):
        ThreadAttachmentRetriever(_UnusableIndexStore(store)).search(
            thread_id=own, query="revenue", attachment_ids=[record.attachment_id]
        )


def test_posting_ordinal_past_the_unit_rows_is_corruption() -> None:
    store, own, _ = _boot()
    record = _seed_ready(store, own)
    store.put_index(
        attachment_id=record.attachment_id,
        version=record.version,
        postings={"revenue": [7]},
    )
    with pytest.raises(AttachmentIndexUnavailableError) as excinfo:
        ThreadAttachmentRetriever(store).search(thread_id=own, query="revenue")
    assert excinfo.value.code == "attachment_index_unavailable"


# ----- legal empties, scoring, ordering, limits -----


def test_legal_query_without_hits_returns_empty() -> None:
    store, own, _ = _boot()
    _seed_ready(store, own)
    assert ThreadAttachmentRetriever(store).search(
        thread_id=own, query="hydrogen"
    ) == ()


@pytest.mark.parametrize("query", ["", "   ", "\t\n "])
def test_blank_query_is_a_legal_empty(query: str) -> None:
    store, own, _ = _boot()
    _seed_ready(store, own)
    # Through the corrupt-postings wrapper too: a blank query never enters
    # the branch, so it cannot be turned into a typed failure either.
    assert ThreadAttachmentRetriever(store).search(thread_id=own, query=query) == ()
    assert ThreadAttachmentRetriever(_UnusableIndexStore(store)).search(
        thread_id=own, query=query
    ) == ()


def test_score_counts_distinct_matched_terms_once() -> None:
    store, own, _ = _boot()
    _seed_ready(store, own)
    chunks = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="revenue revenue million"
    )
    assert [c.score for c in chunks] == [2.0]


def test_merged_order_is_score_desc_then_unit_asc() -> None:
    store, own, _ = _boot()
    first = _seed_ready(
        store,
        own,
        units=(
            ParsedAttachmentUnit(unit=1, text="beta section"),
            ParsedAttachmentUnit(unit=2, text="filler text"),
            ParsedAttachmentUnit(unit=3, text="alpha section"),
        ),
    )
    second = _seed_ready(
        store,
        own,
        units=(
            ParsedAttachmentUnit(unit=1, text="filler text"),
            ParsedAttachmentUnit(unit=2, text="alpha again"),
        ),
    )
    store.put_index(
        attachment_id=first.attachment_id,
        version=first.version,
        postings={"alpha": [3], "beta": [1]},
    )
    store.put_index(
        attachment_id=second.attachment_id,
        version=second.version,
        postings={"alpha": [2], "beta": [1]},
    )
    chunks = ThreadAttachmentRetriever(store).search(
        thread_id=own, query="alpha beta"
    )
    # scores: first.u3=alpha(1), first.u1=beta(1), second.u2=alpha(1),
    # second.u1=beta(1) — all score 1, ordered by unit ascending, then
    # attachment id for the two unit-1 rows.
    assert [(c.source_ref.unit, c.score) for c in chunks] == [
        (1, 1.0),
        (1, 1.0),
        (2, 1.0),
        (3, 1.0),
    ]
    assert chunks[0].source_ref.attachment_id < chunks[1].source_ref.attachment_id


def _seed_wide(store: InMemoryAttachmentStore, thread_id: str, rows: int) -> None:
    units = tuple(
        ParsedAttachmentUnit(unit=n, text=f"dollars figure {n}") for n in range(1, rows + 1)
    )
    _seed_ready(store, thread_id, units=units)


@pytest.mark.parametrize(
    ("limit", "expected"),
    [(None, 8), (0, 8), (-5, 8), (3, 3), (20, 20), (21, 20), (100, 20)],
)
def test_limit_boundaries(limit: int | None, expected: int) -> None:
    store, own, _ = _boot()
    _seed_wide(store, own, 25)
    kwargs = {"thread_id": own, "query": "dollars"}
    if limit is not None:
        kwargs["limit"] = limit
    chunks = ThreadAttachmentRetriever(store).search(**kwargs)
    assert len(chunks) == expected


def test_explicit_limit_none_matches_the_default_window() -> None:
    """``limit=None`` is the SSOT spelling of "unspecified": the same
    window (same rows, same order) as an explicit default of 8."""
    store, own, _ = _boot()
    _seed_wide(store, own, 25)
    retriever = ThreadAttachmentRetriever(store)
    omitted = retriever.search(thread_id=own, query="dollars")
    explicit_default = retriever.search(thread_id=own, query="dollars", limit=8)
    none_default = retriever.search(thread_id=own, query="dollars", limit=None)
    assert none_default == omitted == explicit_default
    assert len(none_default) == 8


# ----- preparation-service delegation -----


def test_preparation_service_search_delegates_to_the_retriever() -> None:
    store, own, other = _boot()
    record = _seed_ready(store, own)
    _seed_ready(store, other)
    service = AttachmentPreparationService(store=store, objects=InMemoryObjectStore())
    chunks = service.search(thread_id=own, query="revenue")
    assert [c.source_ref.attachment_id for c in chunks] == [record.attachment_id]
    assert chunks[0].source_ref.unit == 1
