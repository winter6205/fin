"""Seed facts for the corpus fact table.

T1 ships a single hand-authored seed row so the later
``get_xbrl_fact`` lookup (T5) has at least one tagged fact to
return on the demo AAPL-2023Q3 corpus. Real XBRL tag parsing of the
iXBRL ``.htm`` lands with a later ADR; T1 only needs the typed
catalog to be queryable.

The seed row is the verbatim v1 gold span from
``tests/eval/test_gold.py:98`` — Apple's "Products $ 60,584 $ 63,355
$ 230,901 $ 245,241" on page 4 of the Q3 2023 10-Q. The numeric
value ``383285000000`` (USD 383.285 B, total net sales nine months
ended July 1, 2023) is the row the research-track numeric gate (T5)
will match against a claimed answer.

``seed_facts_for_document`` returns ``None`` for documents that have
no seed yet — the ingest CLI treats ``None`` as "do not write a fact
file", which keeps the tree aligned with the demo's one-fact
catalog. When more tags land, this module is the single place to add
them.
"""

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
                value=383285000000,
                period="2023-Q3-YTD",
                unit="USD",
                scale="thousands",
                source_locator=Locator(
                    document_id=document_id,
                    version=version,
                    page=4,
                ),
            )
        ]
    return None


__all__ = ["seed_facts_for_document"]