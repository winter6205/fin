"""Host-side memory prefetch assembly (NOT a model tool).

`specs/cross-session-operator-memory.md` SC7, SC8, SC9, SC24: when
memory is ON, the host overlays at most three **memory hooks** whose
title or body lexically hits the current user utterance on the user
side of the model-facing payload. Zero hits inject nothing. Already-shown
ids on the same ``thread_id`` are skipped.

The prefetch lives in its own concern (host assembly, not a graph
tool) so the model never decides whether to recall — the operator's
utterance decides it for them. The result is three pieces of state:

* ``select_prefetch`` — pure ranking over the workspace's fact rows.
* ``prefetch_overhead_tokens`` — the ledger delta for the
  ``OccupancyCompactor`` to pass back as ``overhead_tokens`` so
  **memory prefetch** counts toward **session compaction**'s budget
  (SC24 / CONTEXT *recall injection*).

The title-weight-above-body weighting and the ``lowercase + non-
alphanumeric`` tokenisation mirror :func:`MemoryBuffer.recall` so
prefetch and recall agree on what a token is. We do NOT import
``MemoryBuffer._tokens`` (an underscore-private helper); a local copy
keeps this module independent and side-effect-free.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from fin.host.memory_store import MemoryRow


# Research-track prefetch cap per CONTEXT *memory prefetch* / SC8:
# at most three hooks, regardless of how many lexical hits surface.
PREFETCH_MAX_ROWS: int = 3

# Title overlap is weighted above body overlap. Body-only hits still
# count, but the same number of tokens on the title outranks the same
# number on the body. The integers are deliberately simple so a
# reviewer can verify the weighting at a glance.
_TITLE_WEIGHT: int = 2
_BODY_WEIGHT: int = 1


# Same shape as ``MemoryBuffer._tokens`` (lowercase + non-alphanumeric
# split) so prefetch and recall agree on what a token is. Mirrored
# here so this module is importable in isolation — memory_buffer drags
# Postgres and DreamSummarizer, neither of which the assembler needs.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


class PrefetchInputError(Exception):
    """Typed boundary failure for malformed prefetch inputs."""


def _tokens(value: str) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(value)}


def _row_tokens(row: MemoryRow) -> tuple[set[str], set[str]]:
    """Return ``(title_tokens, body_tokens)`` for a fact row.

    The body falls back to ``text`` so legacy rows (pre-body-column)
    still contribute a token set. An empty title or empty body yields
    an empty token set, never raises.
    """
    title = (row.title or "").strip()
    body = (row.body or row.text or "").strip()
    return _tokens(title), _tokens(body)


def _score(
    query_tokens: set[str], title_tokens: set[str], body_tokens: set[str]
) -> int:
    """Lexical score: weighted overlap, zero hit on either field ⇒ zero.

    The spec's "zero token hit on title AND body ⇒ score 0 ⇒ excluded"
    rule is encoded here: a row whose title set AND body set both miss
    the query set contributes 0 and is dropped before ranking. Rows
    with overlap on either field are weighted — title by 2, body by 1.
    """
    title_hit = len(query_tokens & title_tokens)
    body_hit = len(query_tokens & body_tokens)
    if title_hit == 0 and body_hit == 0:
        return 0
    return title_hit * _TITLE_WEIGHT + body_hit * _BODY_WEIGHT


def select_prefetch(
    user_utterance: str,
    rows: Iterable[MemoryRow],
    *,
    shown_ids: set[str] | None = None,
    limit: int = PREFETCH_MAX_ROWS,
) -> list[dict[str, Any]]:
    """Return at most ``limit`` lexical hits as ``(fact_id, title, hook)``.

    Pure function. ``shown_ids`` excludes ids already injected earlier
    on this ``thread_id`` so a fact does not surface twice on the same
    thread (SC9). ``limit`` is bounded above by :data:`PREFETCH_MAX_ROWS`
    even if the caller asks for more (the spec cap is a hard 3).

    Scoring: title tokens weighted 2× above body tokens (1×). A row
    with overlap on title OR body scores ``2*title_hit + body_hit``;
    a row with overlap on NEITHER is excluded (score 0). Ties break
    by ``created_at`` descending then by ``id`` descending so repeated
    calls produce the same selection (deterministic — the SC8 test
    asserts it).
    """
    if not isinstance(user_utterance, str):
        raise PrefetchInputError("user_utterance must be a string")
    if not isinstance(rows, Iterable) or isinstance(rows, (str, bytes)):
        raise PrefetchInputError("rows must be an iterable of MemoryRow")
    if shown_ids is not None and not isinstance(shown_ids, set):
        raise PrefetchInputError("shown_ids must be a set or None")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise PrefetchInputError("limit must be a positive integer")

    excluded = shown_ids or set()
    bounded_limit = min(limit, PREFETCH_MAX_ROWS)
    query_tokens = _tokens(user_utterance)
    if not query_tokens:
        return []

    candidates: list[tuple[int, MemoryRow]] = []
    for row in rows:
        if not isinstance(row, MemoryRow):
            raise PrefetchInputError(
                f"row must be a MemoryRow, got {type(row).__name__}"
            )
        if row.id in excluded:
            continue
        title_tokens, body_tokens = _row_tokens(row)
        score = _score(query_tokens, title_tokens, body_tokens)
        if score > 0:
            candidates.append((score, row))

    # Score desc, then created_at desc, then id desc — total order so
    # equal-score rows break deterministically (test_*) without
    # relying on dict/hash ordering of the underlying store.
    candidates.sort(key=lambda pair: (pair[0], pair[1].created_at, pair[1].id), reverse=True)

    top = candidates[:bounded_limit]
    return [
        {"fact_id": row.id, "title": row.title, "hook": row.hook}
        for _score, row in top
    ]


def prefetch_overhead_tokens(prefetch_rows: Sequence[Mapping[str, Any]] | None) -> int:
    """Return the ledger delta for a prefetch payload, in estimated tokens.

    Uses the same chars/4 approximation as
    :data:`fin.host.session.session_compaction.CHARS_PER_TOKEN` so the budget
    is one number across the host. Callers feed this value back to
    :meth:`OccupancyCompactor.occupancy` as ``overhead_tokens`` so a
    turn with prefetch rows lands higher on the ledger than the same
    turn without (SC24).

    The payload contribution is ``title + hook`` per row (NOT the
    full body — body bytes never enter the model-facing payload, so
    charging them to the ledger would over-count).
    """
    if prefetch_rows is None:
        return 0
    if not isinstance(prefetch_rows, Sequence) or isinstance(
        prefetch_rows, (str, bytes)
    ):
        raise PrefetchInputError("prefetch_rows must be a sequence of mappings")

    # Mirror the session-compaction heuristic. Importing the constant
    # would create a soft dependency the assembler does not otherwise
    # need; a local int keeps the seam explicit and the comment below
    # documents the shared approximation.
    chars_per_token = 4
    total_chars = 0
    for row in prefetch_rows:
        total_chars += len(str(row.get("title", "")))
        total_chars += len(str(row.get("hook", "")))
    return total_chars // chars_per_token


__all__ = [
    "PREFETCH_MAX_ROWS",
    "PrefetchInputError",
    "prefetch_overhead_tokens",
    "select_prefetch",
]