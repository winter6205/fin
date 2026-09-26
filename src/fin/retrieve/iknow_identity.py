"""The content identity iknow-memory computes, mirrored for the seams that
must answer as the service does.

``content_identity`` has exactly one authority: the value the service
stores in ``source_documents.content_identity`` and answers with on the
push response (spec ``corpus-serve-ingest`` Decision 2). fin never treats
a hash of its own as that value, and this module is not an exception to
that rule — it is the **service-side mirror**, used only where fin stands
in for the service (the contract fake :mod:`fin.retrieve.fake_push_port`)
and where a test re-derives what that stand-in claimed. Nothing on the
production read path calls it: :func:`fin.retrieve.push_port.content_identities_of`
reads the pair off the response and never recomputes it.

The formula mirrors ``computeContentIdentity`` in iknow-memory's
``src/tools/ingest.ts``: ``sha256(JSON([body, "normalization-v1",
"parse-v1", "parameters-v1"]))`` — the whole pushed body plus the three
**service-owned** version constants, JSON-serialised compactly and hashed
as UTF-8. Being a mirror, it cannot detect iknow changing its own
constants; that evidence belongs to the cross-repo live test
(``tests/retrieve/test_live_content_identity.py``, opt-in ``-m mcp``),
which compares against the row the real service stored.
"""

from __future__ import annotations

import hashlib
import json
from typing import Final

#: iknow-memory's ``DEFAULT_CONTENT_IDENTITY_VERSIONS``. The document-level
#: identity is keyed on these service-owned constants, not on the rule
#: versions fin carries in its own envelope metadata.
IKNOW_CONTENT_IDENTITY_VERSIONS: Final[tuple[str, ...]] = (
    "normalization-v1",
    "parse-v1",
    "parameters-v1",
)


def compute_iknow_content_identity(body: str) -> str:
    """The identity iknow-memory computes for one pushed body (see module
    doc: a mirror of a value fin only ever reads back)."""
    payload = json.dumps(
        [body, *IKNOW_CONTENT_IDENTITY_VERSIONS],
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "IKNOW_CONTENT_IDENTITY_VERSIONS",
    "compute_iknow_content_identity",
]
