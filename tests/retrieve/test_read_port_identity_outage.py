"""The company read leg answers while the identity database is down.

The corpus identity mapping is ADDITIVE and OPTIONAL: it only adds a
second name for a document the corpus already holds under one. A reader
must therefore never fail because that optional mapping is down — the
text it holds is still the answer, and the outage is visible in the log.

Two outages are exercised, and neither replaces the object under test:

* a hand-written store that raises
  :class:`~fin.corpus.document_identity.DocumentIdentityStoreUnavailableError`
  on every call, standing in for the unreachable database;
* an environment that names a database nobody listens on, so the store
  cannot even be BUILT — the failure happens while the store is resolved.

``unit_text_from_pages`` and the shipped ``read_slice`` leg are the real
ones in both cases.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import pytest

from fin.corpus.document_identity import (
    DocumentIdentityStoreUnavailableError,
    reset_default_document_identity_store,
    set_default_document_identity_store,
)
from fin.corpus.extracted import ExtractedPage, Locator
from fin.retrieve.fake_port import FakeRetrievePort
from fin.retrieve.read_port import unit_text_from_pages

IDENTITY_DSN_ENV = "FIN_DOCUMENT_IDENTITY_DSN"

#: A database nobody listens on: the connection is refused at the TCP
#: level, so the case exercises an unreachable identity database without
#: naming a credential or a real host.
UNREACHABLE_IDENTITY_DSN = "postgresql://fin@127.0.0.1:1/nope"

CANONICAL_ID = "AAPL-2024Q3"
ALIAS_ID = "doc_alias_of_aapl_2024q3"
VERSION = "v" * 8
TEXT = "Apple net sales for the fiscal 2024 third quarter were 95,359 million."
SECOND_TEXT = (
    "Apple net sales for the fiscal 2024 third quarter were 95,359. "
    "The prior quarter was 85,777."
)


class _UnreachableIdentityStore:
    """An identity store that cannot be reached, standing in for an outage."""

    def record_document(self, *, document_id: str, version: str) -> Any:
        raise DocumentIdentityStoreUnavailableError("the mapping store is down")

    def canonical_document_ids(self, document_ids: Any) -> dict[str, str]:
        raise DocumentIdentityStoreUnavailableError("the mapping store is down")


def _pages() -> dict[str, tuple[ExtractedPage, ...]]:
    return {
        CANONICAL_ID: (
            ExtractedPage(
                locator=Locator(document_id=CANONICAL_ID, version=VERSION, unit=1),
                text=TEXT,
                passages=(),
            ),
            ExtractedPage(
                locator=Locator(document_id=CANONICAL_ID, version=VERSION, unit=2),
                text=SECOND_TEXT,
                passages=(),
            ),
        )
    }


@pytest.fixture(autouse=True)
def _the_process_default_store_is_this_cases() -> Iterator[None]:
    """The process-wide identity store is this case's to hand.

    The shipped read leg resolves through the process default, so a case
    that means "the mapping is down" has to say so explicitly, and the
    process has to be put back the way it was found.
    """
    set_default_document_identity_store(None)
    yield
    reset_default_document_identity_store()


def test_the_reader_answers_the_complete_text_while_the_store_is_down() -> None:
    """The reader holds the canonical id: it gets the whole text.

    The mapping is optional, so an outage must not turn a document the
    reader already holds into a miss or an exception.
    """
    store = _UnreachableIdentityStore()
    pages = _pages()

    assert (
        unit_text_from_pages(pages, CANONICAL_ID, VERSION, 1, identity_store=store)
        == TEXT
    )
    assert (
        unit_text_from_pages(pages, CANONICAL_ID, VERSION, 2, identity_store=store)
        == SECOND_TEXT
    )


def test_the_shipped_read_leg_answers_while_the_store_is_unreachable() -> None:
    """The shipped company read leg answers, too.

    ``FakeRetrievePort.read_slice`` is the leg a reader actually calls,
    and it resolves the identifier through the process default: with the
    mapping down it still returns the bounded slice of the text.
    """
    set_default_document_identity_store(_UnreachableIdentityStore())
    port = FakeRetrievePort(pages_by_doc=_pages())

    result = port.read_slice(
        {"document_id": CANONICAL_ID, "version": VERSION, "unit": 1}
    )

    assert result["text"] == TEXT


def test_the_reader_answers_while_the_identity_database_cannot_be_reached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store cannot even be BUILT, and the reader still answers.

    Resolving the store connects to the database, so an unreachable one
    fails before any query is attempted. The read path must not inherit
    that failure.
    """
    monkeypatch.setenv(IDENTITY_DSN_ENV, UNREACHABLE_IDENTITY_DSN)
    reset_default_document_identity_store()

    assert unit_text_from_pages(_pages(), CANONICAL_ID, VERSION, 1) == TEXT


def test_an_alias_lookup_does_not_raise_while_the_store_is_unreachable() -> None:
    """The alias cannot be resolved, so it is a miss — never an exception.

    While the mapping is down the alias is not known to be the canonical
    document, so the lookup misses. That is the hole the mapping closes,
    open for the length of the outage; the reader still answers.
    """
    store = _UnreachableIdentityStore()

    assert (
        unit_text_from_pages(_pages(), ALIAS_ID, VERSION, 1, identity_store=store)
        is None
    )


def test_the_outage_is_visible_in_the_log(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A degraded resolution must not look like "no alias is known".

    The outage needs an operator, so it reaches the log even though the
    reader answers.
    """
    monkeypatch.setenv(IDENTITY_DSN_ENV, UNREACHABLE_IDENTITY_DSN)
    reset_default_document_identity_store()

    with caplog.at_level(logging.WARNING, logger="fin.corpus.document_identity"):
        assert unit_text_from_pages(_pages(), CANONICAL_ID, VERSION, 1) == TEXT

    messages = [record.getMessage() for record in caplog.records]
    assert len(messages) == 1
    assert "DocumentIdentityStoreUnavailableError" in messages[0]
