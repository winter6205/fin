"""[private source excerpt removed]"""

from __future__ import annotations

from fin.corpus._private_fixtures import load_private_fixture

from fin.corpus.extracted import Locator
from fin.corpus.facts import Fact


_AAPL_2023Q3_VERSION = (
    '7b9b54830f070aab56cd842b391dd81d21613aacda2cb92234f43b997930b0ea'
)


def seed_facts_for_document(
    *, document_id: str, version: str
) -> list[Fact] | None:
    """Return the seed fact list for ``document_id``, or ``None`` to
    signal "no seed, do not emit a fact file".

    The seed is keyed off the manifest's ``sha256`` (passed in as
    ``version``) so a drifted PDF cannot be silently re-seeded with a
    stale fact locator — same idempotency rule the rest of the
    corpus slice follows (ADR 0020).
    """
    if document_id == "AAPL-2023Q3" and version == _AAPL_2023Q3_VERSION:
        return [
            Fact(
                concept="us-gaap:Revenues",
                value=load_private_fixture("e8e6fa2502cf2441d4f1862bbbb7d147b7dbd57a3911be363417fe4342e69b94"),
                period="2023-Q3-YTD",
                unit="USD",
                scale="millions",
                source_locator=Locator(
                    document_id=document_id,
                    version=version,
                    unit=4,
                ),
            )
        ]
    return None


__all__ = ["seed_facts_for_document"]