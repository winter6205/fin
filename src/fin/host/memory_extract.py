"""T7 gated auto-extract for cross-session memory.

``specs/cross-session-operator-memory.md`` SC14-SC16 / ADR 0048 turn the
v1 host into a memory-aware agent without turning memory into a second
filing corpus. The gate is CODE, not prompt (``docs/guides/prompt-
development.md``: "否决类能 tokenize 重叠的走代码，不把唯一否决写在 prompt").
The LLM only PROPOSES one candidate; this module decides whether it lands.

Five gates, evaluated top-to-bottom:

1. **Turn count** — fires only when completed turns on this thread_id
   are >= ``EXTRACT_MIN_COMPLETED_TURNS`` (N=3 per SC14). A completed
   turn is a (HumanMessage, AIMessage) pair; a trailing unanswered
   HumanMessage is NOT counted.
2. **This-turn save skip** — when the last AIMessage declared a
   ``save_memory`` tool call this turn, the explicit save already
   persisted the operator's intent, so extract writes 0 (SC14).
3. **LLM proposal** — the injectable ``FactProposer`` returns at most
   one ``FactCandidate``. ``NullExtractor`` (default) returns ``None``,
   which short-circuits the gate to "no proposal, no write".
4. **Deny classes** — code/schema checks reject:

   * filing-claim locators (``[A-Z]{2,5}-\\d{4}Q[1-4]`` regex on the
     candidate body) OR explicit filing/retrieval-evidence phrases
     (``is_filing_claim``),
   * user-profile duplicates (``overlaps_user_profile`` over the
     merged user profile text — a token-set Jaccard against the
     candidate body),
   * identity/soul content (the candidate reads as self-description
     of the agent — first-person self-reference plus agent vocabulary
     in ``is_identity_content``),
   * already-persisted duplicates (same title+hook in the workspace).

5. **Cap 1** — at most one new fact per firing (SC16). Subsequent
   candidates the LLM might surface on the SAME firing are dropped;
   the per-thread cursor is the gate that prevents a second firing
   on the same turn count.

The extractor is a STANDALONE function (``maybe_extract``). An
integration ticket can call it from a turn-completion node or an
HTTP/SSE hook; this module does NOT touch ``graph.py`` /
``workspace_http.py`` / ``memory_dream.py``.

Per-thread cursor
-----------------
A small in-process dictionary keyed by ``(thread_id, workspace)``
holds the last-evaluated completed-turn count for each thread. The
dictionary is process-local — it is the test seam; a production
deployment that wants cross-restart cursor durability is a separate
ticket and lives behind the same ``ExtractCursor`` protocol.

Exceptions
----------
Deny / skip are NORMAL paths — they return a structured
``ExtractResult`` and never raise. A config error (empty
``thread_id``, wrong input types) DOES raise — the caller wired it
wrong, not the gate. An LLM raise is logged and returned as
``("skipped", "llm_error")``; the foreground turn continues (the
explicit-save gate is what guarantees persistence; extract failure
never refuses the operator turn).
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from fin.host.memory_store import HOOK_MAX_CHARS


logger = logging.getLogger(__name__)


# Spec SC14: N=3 completed turns. A trailing unanswered HumanMessage is
# NOT counted (it is the in-progress turn).
EXTRACT_MIN_COMPLETED_TURNS = 3

# Spec SC16: one new fact per firing.
EXTRACT_MAX_PER_FIRING = 1

# Token-set Jaccard threshold for the user-profile duplicate gate.
# 0.6 matches the existing test seed (``test_partial_profile_overlap``)
# and is intentionally on the strict side so the LLM cannot sneak a
# near-duplicate past the gate by paraphrasing.
PROFILE_OVERLAP_THRESHOLD = 0.6


# Spec SC15: filing-claim locator regex. The corpus is ``TICKER-YYYYQn``
# per ADR 0006 (e.g. ``AAPL-2022Q3``). 2-5 uppercase letters cover every
# US equity ticker the corpus will hold.
_FILING_LOCATOR_RE = re.compile(r"\b[A-Z]{2,5}-\d{4}Q[1-4]\b")

# Phrases that mark the candidate as a filing/retrieval evidence
# statement rather than a habit/preference. Lowercased match.
_FILING_PHRASES = (
    "per the filing",
    "per the retrieved",
    "in the filing",
    "in the retrieved document",
    "according to the filing",
    "according to the retrieved document",
    "the retrieved document says",
    "the filing says",
    "the filing states",
    "the retrieved document states",
    "from the retrieved document",
)

# Identity / soul language: first-person self-reference AND
# agent-domain vocabulary. A candidate that reads as "I am ..."
# + (agent, ai, assistant, bot, model, fin) is the agent describing
# itself, not a memory.
_IDENTITY_FIRST_PERSON_RE = re.compile(
    r"\b("
    r"i\s+am|"
    r"i'm|"
    r"im|"
    r"i\s+do\s+not|"
    r"i\s+can|"
    r"i\s+will|"
    r"i\s+am\s+an|"
    r"as\s+an?\s+(?:ai|assistant)"
    r")\b",
    re.IGNORECASE,
)
_IDENTITY_AGENT_VOCAB = (
    "agent",
    "assistant",
    " ai ",
    " ai.",
    " ai,",
    "bot",
    "language model",
    "chatbot",
    "knowledge-base agent",
    "knowledge base agent",
    "workspace agent",
    "fin",
)


# ----- types -----


@dataclass(frozen=True)
class FactCandidate:
    """One proposed memory fact the LLM surfaces from the recent turns.

    Title + body — the same shape :meth:`MemoryBuffer.extract` accepts.
    """

    title: str
    body: str


@dataclass(frozen=True)
class ExtractResult:
    """The typed outcome of one ``maybe_extract`` call.

    ``status`` is one of:

    * ``"extracted"`` — a candidate passed every gate and landed in
      the store; ``fact`` carries the persisted row dict.
    * ``"skipped"`` — a gate chose not to fire (below threshold,
      no proposal, this-turn save, already evaluated, LLM error);
      ``fact`` is ``None`` and ``reason`` is the gate name.
    * ``"denied"`` — a deny class rejected the candidate; ``fact`` is
      ``None`` and ``reason`` is the deny name (``filing_claim`` /
      ``profile_duplicate`` / ``identity_content`` / ``already_persisted``).
    """

    status: str
    reason: str
    fact: dict[str, object] | None

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "reason": self.reason,
            "fact": self.fact,
        }


@runtime_checkable
class FactProposer(Protocol):
    """The LLM seam. Implementations propose ONE candidate per call.

    Returns ``None`` to mean "no extractable fact this turn". An
    implementation may legitimately call the LLM and parse a single
    structured fact out of the response; this protocol only requires
    the shape, so an offline stub works the same way for tests
    (ADR 0044: "injectable seam, dream/test parity").
    """

    def propose(
        self, messages: Sequence[BaseMessage]
    ) -> FactCandidate | None:
        ...


class NullExtractor:
    """The default :class:`FactProposer`. Returns ``None`` — no proposal.

    Used when no LLM is injected so a misconfigured deployment never
    silently calls a real model. Tests pass their own ``_StaticProposer``
    or ``_MultiProposer`` instead.
    """

    def propose(
        self, messages: Sequence[BaseMessage]
    ) -> FactCandidate | None:
        del messages
        return None


# ----- cursor -----


class ExtractCursor(Protocol):
    """Per-thread (last evaluated completed-turn count) seam.

    The in-process implementation holds the counts in a
    ``threading.Lock``-guarded dict so concurrent calls on the same
    thread_id are race-free. A future Postgres-backed implementation
    sits behind the same protocol; this module only needs the gate
    semantics, not the storage shape.
    """

    def load(self, thread_id: str, workspace: str) -> int:
        """Return the last evaluated completed-turn count for this thread."""
        ...

    def store(self, thread_id: str, workspace: str, completed_turns: int) -> None:
        """Record a new last-evaluated completed-turn count."""
        ...


class InProcessExtractCursor:
    """Process-local cursor for the auto-extract gate.

    Mirrors :class:`InProcessDreamCursorStore` in spirit: a guarded
    dict, no cross-restart durability. The module-level
    :data:`_DEFAULT_EXTRACT_CURSOR` is the shared default
    :func:`maybe_extract` falls back to so two consecutive calls on
    the same ``thread_id`` / ``workspace`` see the prior cursor write
    — a fresh per-call cursor would make the "already evaluated"
    branch unreachable in production. Tests that want isolation pass
    their own ``InProcessExtractCursor()``.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = threading.Lock()

    def load(self, thread_id: str, workspace: str) -> int:
        with self._lock:
            return self._counts.get((thread_id, workspace), 0)

    def store(
        self, thread_id: str, workspace: str, completed_turns: int
    ) -> None:
        with self._lock:
            self._counts[(thread_id, workspace)] = int(completed_turns)

    def reset(self) -> None:
        """Drop every tracked thread.

        Test seam — the module-level :data:`_DEFAULT_EXTRACT_CURSOR`
        is shared across tests in a single process, and a long suite
        would otherwise accumulate (thread_id, workspace) entries that
        silently skew ``already_evaluated`` coverage. Tests call this
        in setup; production never does.
        """
        with self._lock:
            self._counts.clear()


# Module-level default cursor. ``maybe_extract`` falls back to this
# when no explicit cursor is injected, so two consecutive calls on
# the same ``thread_id`` / ``workspace`` share state (the "already
# evaluated" branch is reachable in production). Tests that want
# isolation pass their own :class:`InProcessExtractCursor`.
_DEFAULT_EXTRACT_CURSOR: ExtractCursor = InProcessExtractCursor()


# ----- the gate -----


def count_completed_turns(
    state_or_messages: Any,
) -> int:
    """Return the number of completed turns in ``state_or_messages``.

    A completed turn is a (HumanMessage, AIMessage) pair where the
    AIMessage is the trailing reply (T3 / SC14). An unanswered
    trailing HumanMessage is the in-progress turn and is NOT counted
    — the extract gate fires after the operator has received a model
    reply.

    ``state_or_messages`` accepts either the LangGraph ``AgentState``
    dict (looks up ``state["messages"]``) or a bare list of
    ``BaseMessage``. Both shapes appear in the codebase; callers
    pass whichever they have at the call site.
    """
    if state_or_messages is None:
        return 0
    if isinstance(state_or_messages, Mapping):
        messages = list(state_or_messages.get("messages") or [])
    elif isinstance(state_or_messages, list):
        messages = state_or_messages
    else:
        raise ValueError(
            "count_completed_turns expects a mapping with 'messages' or a "
            f"bare list, got {type(state_or_messages).__name__}"
        )

    count = 0
    pending_human = False
    for message in messages:
        if not isinstance(message, BaseMessage):
            # Anything that is not a BaseMessage (a list of dicts, a
            # tuple, ...) is treated as not-a-turn and breaks the
            # pair-counting; the caller is mis-wired.
            pending_human = False
            continue
        if isinstance(message, HumanMessage):
            pending_human = True
            continue
        if isinstance(message, AIMessage) and pending_human:
            count += 1
            pending_human = False
        else:
            # AIMessage without a preceding unanswered human, or any
            # other message type — does not contribute to the
            # completed-turn count. LangGraph keeps the human/ai pair
            # adjacent in practice (system / tool messages between
            # them are unusual), so a more permissive "AI after a
            # Human within the last few messages" is unnecessary.
            pending_human = False
    return count


def _this_turn_saved(messages: Sequence[BaseMessage]) -> bool:
    """True iff the last AIMessage declared a ``save_memory`` tool call.

    The extract path is gated on this so a manual save this turn
    already persists the operator's intent and extract must write
    0 (SC14).
    """
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            for call in message.tool_calls or ():
                if (call.get("name") or "") == "save_memory":
                    return True
            return False
        # Only walk back through AIMessages. A ToolMessage between
        # the AIMessage and a prior HumanMessage is the
        # save_memory result and confirms the save happened — but
        # we already detected it from the AIMessage tool_calls.
        if isinstance(message, HumanMessage):
            return False
    return False


def extract_filing_claim_locator(text: str) -> str | None:
    """Return the first filing-claim locator in ``text``, or ``None``.

    Public helper so tests can assert the regex directly. The gate
    uses :func:`is_filing_claim`, which combines this locator check
    with the explicit-phrase check.
    """
    if not isinstance(text, str):
        return None
    match = _FILING_LOCATOR_RE.search(text)
    return match.group(0) if match else None


def is_filing_claim(text: str) -> bool:
    """True iff ``text`` reads as a retrieve-able filing claim.

    Two ways to qualify, both code gates (SC15 / prompt-development
    guide: "否决类能 tokenize 重叠的走代码"):

    * contains a filing-claim locator (``AAPL-2022Q3``-shaped token);
    * references the filing / retrieved document as evidence
      ("per the filing", "the filing states", ...).
    """
    if not isinstance(text, str):
        return False
    if extract_filing_claim_locator(text) is not None:
        return True
    lowered = text.casefold()
    return any(phrase in lowered for phrase in _FILING_PHRASES)


def is_identity_content(text: str) -> bool:
    """True iff ``text`` reads as the agent describing its own identity.

    Both signals are required: a first-person self-reference AND
    agent-domain vocabulary in the same text. "I prefer bullet
    points" is first-person but not identity; "fin is an agent" is
    identity vocabulary but lacks first-person framing. The pair
    together rejects the agent-soul denial class (SC15).
    """
    if not isinstance(text, str):
        return False
    if not _IDENTITY_FIRST_PERSON_RE.search(text):
        return False
    lowered = f" {text.casefold()} "
    return any(vocab in lowered for vocab in _IDENTITY_AGENT_VOCAB)


def normalize_for_overlap(text: str) -> str:
    """Fold whitespace, lowercase, strip punctuation-to-space.

    Used by the profile-duplicate gate so "bullet-point" and "bullet
    point" tokenize the same way the lexical recall does
    (``_tokens`` in ``memory_buffer``).
    """
    if not isinstance(text, str):
        return ""
    folded = re.sub(r"\s+", " ", text.casefold()).strip()
    # Collapse hyphen / underscore to space so "bullet-point" and
    # "bullet point" match.
    folded = re.sub(r"[-_/]+", " ", folded)
    return folded


def _token_set(text: str) -> set[str]:
    return {token for token in re.findall(r"\w+", text) if token}


def overlaps_user_profile(
    candidate_text: str,
    profile_text: str,
    *,
    threshold: float = PROFILE_OVERLAP_THRESHOLD,
) -> bool:
    """True iff ``candidate_text`` is a near-duplicate of ``profile_text``.

    Token-set Jaccard with a strict default of 0.6. The threshold
    matches the existing SC15 test seed (``test_partial_profile_overlap``)
    and is intentionally on the strict side so the LLM cannot sneak
    a paraphrase past the gate.
    """
    if not isinstance(candidate_text, str) or not isinstance(profile_text, str):
        return False
    if not profile_text.strip():
        return False
    candidate_tokens = _token_set(normalize_for_overlap(candidate_text))
    profile_tokens = _token_set(normalize_for_overlap(profile_text))
    if not profile_tokens:
        return False
    intersection = len(candidate_tokens & profile_tokens)
    union = len(candidate_tokens | profile_tokens)
    if union == 0:
        return False
    return (intersection / union) >= threshold


def _hook_of(text: str) -> str:
    """First non-empty line of ``text``, folded, truncated to ``HOOK_MAX_CHARS``.

    Mirrors :class:`MemoryRow`'s hook projection so the duplicate
    check is over the same surface that prefetch and recall use.
    """
    if not isinstance(text, str):
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            folded = re.sub(r"\s+", " ", stripped)
            return folded[:HOOK_MAX_CHARS]
    return ""


def _already_persisted(
    candidate: FactCandidate,
    memory_port: Any,
    workspace: str,
) -> bool:
    """True iff ``(title, hook)`` already exists in the workspace store.

    A candidate is "already persisted" when the same title AND the
    same first-non-empty-body-line hook are in the workspace. A new
    title with the same hook is a different fact (paraphrased restate
    vs the existing habit) — the gate must not block that. A title
    match with no hook overlap is a topic-rehash, also not blocked.
    """
    hook = _hook_of(candidate.body)
    if not hook:
        return False
    try:
        rows = memory_port.entries(include_disabled=False, workspace=workspace)
    except Exception:  # noqa: BLE001 — port shape varies; entries() is optional
        return False
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("title") == candidate.title and row.get("hook") == hook:
            return True
    return False


def maybe_extract(
    state_or_messages: Any,
    memory_port: Any,
    *,
    thread_id: str,
    workspace: str,
    user_profile_store: Any | None = None,
    llm: FactProposer | None = None,
    cursor: ExtractCursor | None = None,
) -> ExtractResult:
    """Evaluate the auto-extract gate once for ``thread_id``.

    Parameters
    ----------
    state_or_messages
        Either an ``AgentState`` dict (``{"messages": [...]}``) or a
        bare list of ``BaseMessage``. The graph factory has the
        former; tests / standalone callers have the latter.
    memory_port
        The active :class:`MemoryPort`. Used both as the persistence
        target (callers without LLM propose nothing; callers with LLM
        land at most one fact per firing) and as the source of the
        already-persisted check.
    thread_id
        The conversation key. Empty / non-string raises a config
        error — the caller is mis-wired.
    workspace
        The workspace key. Persisted rows are workspace-scoped
        (spec SC6); the duplicate check is too.
    user_profile_store
        Optional :class:`UserProfileStore`. When ``None`` the
        profile-duplicate deny is skipped (no profile to compare
        against — same fallback the T6 save tool uses for fresh
        workspaces).
    llm
        The :class:`FactProposer` that proposes one candidate from
        the recent turns. ``None`` defaults to :class:`NullExtractor`,
        which proposes nothing.
    cursor
        Optional :class:`ExtractCursor`. ``None`` defaults to a fresh
        :class:`InProcessExtractCursor`. Tests pass an explicit
        cursor to share state across calls; production wires one per
        process.

    Returns
    -------
    ExtractResult
        ``status`` is ``"extracted"`` / ``"skipped"`` / ``"denied"``;
        ``reason`` names the gate. Never raises for a normal deny or
        skip — only config errors (empty ``thread_id``, wrong input
        shapes) raise ``ValueError``.
    """
    if not isinstance(thread_id, str) or not thread_id.strip():
        raise ValueError("maybe_extract requires a non-empty thread_id")
    if not isinstance(workspace, str):
        raise ValueError("maybe_extract requires a string workspace")
    proposer = llm if llm is not None else NullExtractor()
    cursor_obj = cursor if cursor is not None else _DEFAULT_EXTRACT_CURSOR

    messages = _resolve_messages(state_or_messages)
    completed_turns = count_completed_turns(messages)

    # The five gates are split into three helpers — pre-candidate
    # gates (skip-only), the candidate-producing proposal step, and
    # post-candidate deny gates — so ``maybe_extract`` stays a flat
    # pipeline that reads top-to-bottom. Each helper returns the
    # result directly when the gate fires, or ``None`` to mean
    # "gate passed, keep going".

    pre = _evaluate_pre_candidate_gates(
        messages=messages,
        completed_turns=completed_turns,
        thread_id=thread_id,
        workspace=workspace,
        cursor=cursor_obj,
    )
    if pre is not None:
        return pre

    candidate = _propose_candidate(proposer, messages)
    if isinstance(candidate, ExtractResult):
        return candidate

    post = _evaluate_post_candidate_gates(
        candidate=candidate,
        memory_port=memory_port,
        workspace=workspace,
        user_profile_store=user_profile_store,
    )
    if post is not None:
        return post

    return _persist_candidate(
        candidate=candidate,
        memory_port=memory_port,
        thread_id=thread_id,
        workspace=workspace,
        completed_turns=completed_turns,
        cursor=cursor_obj,
    )


def _evaluate_pre_candidate_gates(
    *,
    messages: list[BaseMessage],
    completed_turns: int,
    thread_id: str,
    workspace: str,
    cursor: ExtractCursor,
) -> ExtractResult | None:
    """Gates that run BEFORE the LLM is asked (SC14).

    Returns the gate's ``ExtractResult`` when the gate fires,
    ``None`` to mean "every pre-gate passed".
    """
    if completed_turns < EXTRACT_MIN_COMPLETED_TURNS:
        return ExtractResult(
            status="skipped", reason="below_turn_threshold", fact=None
        )
    if _this_turn_saved(messages):
        return ExtractResult(status="skipped", reason="this_turn_save", fact=None)
    last_evaluated = cursor.load(thread_id, workspace)
    if completed_turns <= last_evaluated:
        return ExtractResult(status="skipped", reason="already_evaluated", fact=None)
    return None


def _propose_candidate(
    proposer: FactProposer, messages: list[BaseMessage]
) -> FactCandidate | ExtractResult:
    """Ask the LLM for one candidate.

    Returns the candidate on success. Returns an ``ExtractResult``
    with the gate's skip reason when the LLM errors, returns
    nothing, or returns a malformed value (which raises).
    """
    try:
        candidate = proposer.propose(list(messages))
    except Exception as exc:  # noqa: BLE001 — LLM is an injected seam
        # LLM outage is a SKIPPED normal path; the explicit-save
        # gate is what guarantees persistence (SC14), extract
        # failure must not refuse the operator turn.
        logger.warning("auto-extract LLM failed: %s", exc)
        return ExtractResult(status="skipped", reason="llm_error", fact=None)

    if candidate is None:
        return ExtractResult(status="skipped", reason="no_proposal", fact=None)

    if not isinstance(candidate, FactCandidate):
        # A proposer that returned something other than None / FactCandidate
        # is mis-wired — the LLM seam contract is "FactCandidate | None".
        raise ValueError(
            "FactProposer.propose must return FactCandidate or None; "
            f"got {type(candidate).__name__}"
        )

    if not candidate.title.strip() or not candidate.body.strip():
        return ExtractResult(status="skipped", reason="empty_candidate", fact=None)

    return candidate


def _evaluate_post_candidate_gates(
    *,
    candidate: FactCandidate,
    memory_port: Any,
    workspace: str,
    user_profile_store: Any | None,
) -> ExtractResult | None:
    """Deny classes that run AFTER the LLM proposes (SC15).

    Returns the gate's ``ExtractResult`` when a deny class fires,
    ``None`` to mean "every deny class passed".
    """
    if is_filing_claim(candidate.body) or is_filing_claim(candidate.title):
        return ExtractResult(status="denied", reason="filing_claim", fact=None)
    if is_identity_content(candidate.body):
        return ExtractResult(status="denied", reason="identity_content", fact=None)
    if user_profile_store is not None and _overlaps_profile(
        candidate.body, user_profile_store
    ):
        return ExtractResult(status="denied", reason="profile_duplicate", fact=None)
    if _already_persisted(candidate, memory_port, workspace):
        return ExtractResult(status="denied", reason="already_persisted", fact=None)
    return None


def _overlaps_profile(candidate_body: str, user_profile_store: Any) -> bool:
    """Compare the candidate body against the merged user profile.

    A profile-read failure is logged as an empty profile, so the
    gate degrades to "skip the deny" — the gate is best-effort and
    must not refuse the operator turn.
    """
    try:
        profile_text = user_profile_store.read_merged()
    except Exception:  # noqa: BLE001 — store shape varies
        profile_text = ""
    return overlaps_user_profile(candidate_body, profile_text)


def _persist_candidate(
    *,
    candidate: FactCandidate,
    memory_port: Any,
    thread_id: str,
    workspace: str,
    completed_turns: int,
    cursor: ExtractCursor,
) -> ExtractResult:
    """Persist the candidate and advance the cursor (SC16 cap-1).

    This is the ONLY successful path through the gate; landing here
    means exactly one fact per firing.
    """
    persisted = memory_port.extract(
        candidate.body,
        metadata={"title": candidate.title, "thread_id": thread_id},
        title=candidate.title,
        workspace=workspace,
    )
    cursor.store(thread_id, workspace, completed_turns)
    # ``persisted`` is the port envelope ``{"status": ..., "memories":
    # [{...row...}]}``. ``fact`` is the row dict so the caller sees
    # the same shape ``MemoryBuffer.entries()`` returns — the
    # canonical "persisted fact" view.
    persisted_row = _extract_persisted_row(persisted)
    return ExtractResult(status="extracted", reason="ok", fact=persisted_row)


def _extract_persisted_row(persisted: Any) -> dict[str, object] | None:
    """Pull the canonical row dict out of a port extract envelope.

    The port returns ``{"status": ..., "memories": [{...row...}]}``;
    ``fact`` is the row dict so callers see the same shape
    ``MemoryBuffer.entries()`` returns. Returns ``None`` on a
    non-conformant envelope (a port that returned a malformed shape
    is a code error, not a typed EXIT).
    """
    if not isinstance(persisted, dict):
        return None
    memories = persisted.get("memories")
    if not isinstance(memories, list) or not memories:
        return None
    if not isinstance(memories[0], dict):
        return None
    return dict(memories[0])


def _resolve_messages(state_or_messages: Any) -> list[BaseMessage]:
    """Coerce ``state_or_messages`` into a list of ``BaseMessage``.

    A mapping without ``messages`` raises — the gate cannot run
    blind; a list passes through. Any other shape raises too.
    """
    if state_or_messages is None:
        raise ValueError("maybe_extract requires a non-empty state/messages")
    if isinstance(state_or_messages, Mapping):
        raw = state_or_messages.get("messages")
        if not raw:
            raise ValueError(
                "maybe_extract requires state['messages'] to be a non-empty list"
            )
        return list(raw)
    if isinstance(state_or_messages, list):
        return state_or_messages
    raise ValueError(
        "maybe_extract expects a state dict or a messages list; "
        f"got {type(state_or_messages).__name__}"
    )


__all__ = [
    "EXTRACT_MAX_PER_FIRING",
    "EXTRACT_MIN_COMPLETED_TURNS",
    "ExtractCursor",
    "ExtractResult",
    "FactCandidate",
    "FactProposer",
    "InProcessExtractCursor",
    "NullExtractor",
    "PROFILE_OVERLAP_THRESHOLD",
    "count_completed_turns",
    "extract_filing_claim_locator",
    "is_filing_claim",
    "is_identity_content",
    "maybe_extract",
    "normalize_for_overlap",
    "overlaps_user_profile",
]
