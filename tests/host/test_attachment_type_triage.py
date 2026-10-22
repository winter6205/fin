"""A ``.md`` upload is a supported type, and a never-asked row is not a success.

The page-lane suffix lists are not the supported-type list. A name outside
both takes the text lane, which decodes UTF-8 and freezes one unit. A
``.md`` is that shape: refusing it as ``attachment_type_unsupported``,
adding a Markdown parser, or widening the page-lane lists to cover it
would fail this file.

The content ladder is not silent: bytes that are not UTF-8 get the named
``attachment_not_text`` refusal, and blank text gets
``attachment_empty_text``.

Hermetic: in-memory stores, no page reader and no vision port.
"""

from __future__ import annotations

import pytest

from fin.corpus.object_store import InMemoryObjectStore
from fin.host.attachment.attachment_prepare import (
    ATTACHMENT_EMPTY_TEXT,
    ATTACHMENT_NOT_TEXT,
    ATTACHMENT_TYPE_UNSUPPORTED,
    AttachmentPreparationService,
)
from fin.host.attachment.attachment_store import InMemoryAttachmentStore
from fin.host.thread_store import InMemoryThreadStore

POLICY_MD = (
    "# ACME Corporation - Corporate Travel and Expense Policy\n"
    "\n"
    "Employees may claim up to 75 US dollars per person for a working business"
    " lunch and up to 110 US dollars per person for a working business dinner.\n"
)


@pytest.fixture
def threads() -> InMemoryThreadStore:
    return InMemoryThreadStore()


@pytest.fixture
def service(threads: InMemoryThreadStore) -> AttachmentPreparationService:
    """The text lane only — no page ports, no vision, no budgets."""
    return AttachmentPreparationService(
        store=InMemoryAttachmentStore(threads), objects=InMemoryObjectStore()
    )


def test_markdown_is_a_supported_type_and_freezes_its_text(
    service: AttachmentPreparationService, threads: InMemoryThreadStore
) -> None:
    """A ``.md`` reaches ``ready`` with one frozen unit."""
    thread = threads.create_thread(title="markdown upload")
    accepted = service.accept(
        thread_id=thread.id,
        filename="expense-policy-001.md",
        payload=POLICY_MD.encode(),
    )

    settled = service.prepare(accepted.attachment_id)

    assert settled.status == "ready", settled
    assert settled.searchable is True, settled
    assert settled.failure_reason is None, settled
    assert settled.version == 1, settled
    assert settled.failure_reason != ATTACHMENT_TYPE_UNSUPPORTED, settled
    units = service.store.units(settled.attachment_id, settled.version)
    assert len(units) == 1, units
    assert "75 US dollars" in units[0].text, units


def test_markdown_with_undecodable_bytes_is_named_not_silent(
    service: AttachmentPreparationService, threads: InMemoryThreadStore
) -> None:
    """Bytes that do not decode are ``attachment_not_text``, not a silent stall."""
    thread = threads.create_thread(title="undecodable markdown")
    accepted = service.accept(
        thread_id=thread.id,
        filename="broken.md",
        payload=b"\xff\xfe\x00\x01 not utf-8",
    )

    settled = service.prepare(accepted.attachment_id)

    assert settled.status == "failed", settled
    assert settled.failure_reason == ATTACHMENT_NOT_TEXT, settled
    assert settled.searchable is False, settled
    assert service.objects.get(accepted.object_key) == b"\xff\xfe\x00\x01 not utf-8"


def test_blank_markdown_is_empty_before_it_is_anything_else(
    service: AttachmentPreparationService, threads: InMemoryThreadStore
) -> None:
    """Blank text is ``attachment_empty_text``, never a published ``ready``."""
    thread = threads.create_thread(title="blank markdown")
    accepted = service.accept(
        thread_id=thread.id, filename="blank.md", payload="   \n\t\n".encode()
    )

    settled = service.prepare(accepted.attachment_id)

    assert settled.status == "failed", settled
    assert settled.failure_reason == ATTACHMENT_EMPTY_TEXT, settled
    assert settled.searchable is False, settled
