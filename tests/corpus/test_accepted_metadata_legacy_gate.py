"""Every accept persists ``upload_metadata_accepted``; the legacy leg recovers.

There is one accept contract: ``submit_durable`` writes the
``upload_metadata_accepted`` event before the worker runs, whether the
caller is the public file-only flow or the trusted explicit-ID seam. A
job accepted BEFORE this contract existed has no such row, so the worker
keeps the old recovery path (``_accepted_for_run`` returning ``None``):
the run splits its ``{TICKER}-{PERIOD}`` id back into ``(company,
period)``. ``tests/corpus/test_ingest_durable_restart.py`` carries the
byte-identical restart contract.
"""

from __future__ import annotations

import hashlib
from typing import Any

from fin.corpus.catalog_publication import (
    UPLOAD_METADATA_ACCEPTED,
    read_accepted_metadata,
)
from fin.corpus.ingest_endpoint import IngestEndpoint
from fin.corpus.ingest_state import InMemoryIngestStateStore, IngestState
from fin.corpus.metadata_store import InMemoryMetadataStore
from fin.corpus.object_store import InMemoryObjectStore
from fin.retrieve.fake_push_port import FakePushPort

_TEXT = b"# Handbook\n\nOnboarding starts with a laptop and an account.\n"


class _Stack:
    """One endpoint over the three accept-time ports, all reachable."""

    def __init__(self, **overrides: Any) -> None:
        self.objects = overrides.pop("objects", None) or InMemoryObjectStore()
        self.pointers = (
            overrides.pop("pointers", None) or InMemoryMetadataStore()
        )
        self.states = (
            overrides.pop("states", None) or InMemoryIngestStateStore()
        )
        self.seen: list[Any] = []
        self.endpoint = IngestEndpoint(
            push_port=FakePushPort(),
            object_store=self.objects,
            metadata_store=self.pointers,
            state_store=self.states,
            corpus_scope_id="fin-default",
            on_ingested=self.seen.append,
            **overrides,
        )

    def seed_legacy_pending_job(
        self,
        *,
        document_id: str,
        data: bytes,
        content_type: str = "text/plain",
    ) -> None:
        """Seed a PRE-CONTRACT accept: object + pointer + pending job, no event.

        ``submit_durable`` now always writes ``upload_metadata_accepted``,
        so a job accepted before this contract cannot be produced through
        the public door. Writing the three durable items directly reaches
        exactly the state such a job is found in — and, crucially, leaves
        NO accepted-metadata row, so the run must fall back to the ``None``
        leg.
        """
        version = hashlib.sha256(data).hexdigest()
        pointer = self.objects.put(
            f"filings/{document_id}/{version}", data, content_type=content_type
        )
        self.pointers.put_pointer(document_id, pointer)
        self.states.create_job(document_id)

    def actions(self, document_id: str) -> list[str]:
        return [row.action for row in self.states.list_audit(document_id)]


# ----- the accept gate: the event is always written --------------------


def test_a_file_only_accept_still_writes_the_accepted_metadata_event() -> None:
    """The file is the only required input; the redo input is durable."""
    stack = _Stack()

    submission = stack.endpoint.submit_durable(_TEXT, filename="a.md")

    assert stack.actions(submission.document_id) == [
        "job_created",
        UPLOAD_METADATA_ACCEPTED,
    ]


def test_an_accept_carrying_company_and_period_writes_the_event() -> None:
    """Fields do not make the door: the one flow always records the event."""
    stack = _Stack()

    submission = stack.endpoint.submit_durable(
        _TEXT,
        filename="a.md",
        company="Acme",
        period="2024Q1",
    )

    assert stack.actions(submission.document_id) == [
        "job_created",
        UPLOAD_METADATA_ACCEPTED,
    ]
    accepted = read_accepted_metadata(
        stack.states.list_audit(submission.document_id)
    )
    assert accepted is not None
    assert accepted.company == "Acme"
    assert accepted.period == "2024Q1"


def test_the_trusted_explicit_id_seam_also_records_the_event() -> None:
    """A caller-chosen id is honored on the trusted internal seam, and it
    too persists its redo input — it is not a second public mode, but it
    goes through the same accept."""
    stack = _Stack()

    submission = stack.endpoint.submit_durable(
        _TEXT, declared_format="txt", document_id="MSFT-2023Q1"
    )

    assert submission.document_id == "MSFT-2023Q1"
    assert stack.actions(submission.document_id) == [
        "job_created",
        UPLOAD_METADATA_ACCEPTED,
    ]


# ----- the legacy gate: no accepted row, recover from the id -----------


def test_a_legacy_pending_job_recovers_its_company_and_period() -> None:
    """A job accepted before this contract has no accepted row.

    ``_accepted_for_run`` returns ``None``, so the run splits its
    ``{TICKER}-{PERIOD}`` id back into ``(company, period)`` and hands the
    recovered pair to the catalog hook.
    """
    stack = _Stack()
    stack.seed_legacy_pending_job(document_id="MSFT-2023Q1", data=_TEXT)

    outcome = stack.endpoint.run_durable("MSFT-2023Q1")

    assert outcome.state is IngestState.READY
    assert "upload_metadata_accepted" not in stack.actions("MSFT-2023Q1")
    assert len(stack.seen) == 1
    assert stack.seen[0].company == "MSFT"
    assert stack.seen[0].period == "2023Q1"


def test_an_opaque_legacy_id_yields_no_company() -> None:
    """An id that is not a ``{TICKER}-{PERIOD}`` pair is never guessed at.

    A wrong company is worse than an absent one — the filing would become
    searchable under a name that does not own it — so the run publishes
    the row with no company rather than reading ``0000320193`` as a ticker.
    """
    stack = _Stack()
    stack.seed_legacy_pending_job(
        document_id="0000320193-24-000006", data=_TEXT
    )

    outcome = stack.endpoint.run_durable("0000320193-24-000006")

    assert outcome.state is IngestState.READY
    assert len(stack.seen) == 1
    assert stack.seen[0].company is None
    assert stack.seen[0].period is None
