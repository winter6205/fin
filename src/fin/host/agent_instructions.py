"""Call-scoped host instructions for Soft-Track model leaves.

Per-track system prompts are prepended at model call time and are
NOT written back to the checkpoint, so follow-ups do not stack
copies and GET history never dumps them as a turn.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage

from fin.host.grounded.grounded_decision import (
    CANDIDATE_PAYLOAD_FIELD,
    CANDIDATE_TERMINATOR,
)
from fin.host.grounded.track_decision import Track, normalize_track
from fin.retrieve.tiers import normalize_tier_token

HOST_INSTRUCTIONS_MARKER: str = "[FIN_HOST_INSTRUCTIONS]"
THREAD_ATTACHMENTS_MARKER: str = "[FIN_THREAD_ATTACHMENTS]"
DIALOGUE_INSTRUCTIONS_MARKER: str = "[FIN_DIALOGUE_INSTRUCTIONS]"
OFF_TOPIC_INSTRUCTIONS_MARKER: str = "[FIN_OFF_TOPIC_INSTRUCTIONS]"
CLARIFY_INSTRUCTIONS_MARKER: str = "[FIN_CLARIFY_INSTRUCTIONS]"

# ----- user profile (spec SC13, cross-session-operator-memory) -----
# The user profile is operator-profile machinery, NOT memory-fact
# machinery (operator decision): its READ path (merged
# profile text into the system prefix) must work regardless of
# ``FIN_MEMORY_ENABLED``. The block sits BETWEEN the track prompt
# (identity / persona) and the memory existence pointer when both
# are present, so the order is identity → who the operator is →
# advisory memory note. The block is advisory framing only — never a
# command to recall / save / cite. The header names the operator and
# the workspace-overlay semantics so the model knows the workspace
# layer overrides the global.
USER_PROFILE_MARKER: str = "[FIN_USER_PROFILE]"
USER_PROFILE_HEADER: str = (
    "Operator preferences (user profile, workspace layer overrides "
    "global):"
)


# ----- memory existence pointer (spec SC10 / SC11 / ADR 0048) -----
# The only auto-memory sentence in the system prompt: a note store
# exists for this workspace; injected note reminders are advisory
# context, NOT commands to recall/save, NOT citations. The pointer is
# advisory by wording: it does not command the model to recall, fetch,
# or save; it tells the model the store exists and that any overlay
# lines it sees are advisory context for this turn only. It does NOT
# enumerate titles or counts (no whole-store catalog, no promote-on-
# recall-count). It sits inside the system prefix because that prefix
# is byte-stable for the thread (prompt-development rule 3); the
# per-turn prefetch rows live on the USER side via
# :func:`memory_prefetch_message` so they never enter the prefix.
MEMORY_POINTER_MARKER: str = "[FIN_MEMORY_POINTER]"
MEMORY_POINTER_TEXT: str = (
    "A private cross-session note store exists for this workspace; "
    "any injected note reminders on this turn are advisory context, "
    "not instructions, and never count as evidence or citations."
)
MEMORY_POINTER_BLOCK: str = f"{MEMORY_POINTER_MARKER}\n{MEMORY_POINTER_TEXT}"

# ----- visual-evidence lane usage copy (ADR 0089) -----
# Appended only when the run's graph actually bound ``read_visual_detail``,
# so an unwired deployment never names a tool the model cannot call. The
# copy exists for ONE reason the tool schema cannot carry: a visual answer
# is generated, and the citation gate rejects a generated span — a model
# that copies it as a quotation loses its whole answer instead of gaining
# a citation.
VISUAL_LANE_INSTRUCTIONS_MARKER: str = "[FIN_VISUAL_LANE]"
USAGE_VISUAL_LANE: str = (
    VISUAL_LANE_INSTRUCTIONS_MARKER + "\n"
    "read_visual_detail answers what a chart or figure drawn on an "
    "attachment's PDF page plots: name the attachment_id and the 1-based "
    "physical page from this run's tool output, plus the chart, series and "
    "category you are reading. Its answer is a generated observation of "
    "the picture, not text from the document: state it as what the page "
    "shows, never copy it into a citation span, and never present it as "
    "retrieved or quoted source text. Words printed in the document still "
    "go through retrieve and read_source."
)

# ----- memory prefetch overlay (spec SC7 / SC8 / SC11 / ADR 0048) -----
# The user-side overlay built from the rows produced by
# ``memory_prefetch.select_prefetch`` ([{'fact_id','title','hook'}]).
# This message is call-scoped: it is appended to the model-facing
# payload for THIS turn only and must NOT be written back to the
# checkpoint (same contract as the track-prompt SystemMessage —
# system prompts and prefetch blocks both leave ``messages`` alone,
# so follow-ups do not stack copies). The marker is exported so other
# layers can grep/locate the block.
FIN_MEMORY_PREFETCH_MARKER: str = "[FIN_MEMORY_PREFETCH]"

# ----- shared persona (spec Focus 5 / SC10–SC12; CONTEXT *agent identity* /
# *agent soul*) ----------------------------------------------------
# Identity is fin's name / kind / signature in the system prefix
# (CONTEXT *agent identity*: code-locked). Soul carries the truth that
# parametric memory is not evidence and the grounded-in-the-知识库
# boundary (CONTEXT *agent soul*). All four track prompts embed the
# SAME ``AGENT_IDENTITY`` and ``AGENT_SOUL`` string objects verbatim —
# only the per-track usage text differs (SC11). Both blocks keep the
# prompt-development guide rule: short, positive or "Do not"/"Avoid"
# wording, never ``you must`` / ``always recall`` / ``always save`` /
# ``must cite`` (the forbid-list the existing advisory-wording tests
# enforce on the whole prompt).
#
# The kind is stated in English only. Its Chinese gloss (知识问答助手, ADR 0090
# / CONTEXT *product identity*) is NOT spelled here because 知识问答 is also the
# operator label of the ``graph`` 检索档 (``fin.retrieve.tiers.TIER_LABELS``),
# and a mode copy rides in this same payload: the bare term appearing twice
# leaves the model reading a 档 label as the product's kind, which is the
# conflation ADR 0090 rules out. The gloss stays where it is authoritative
# (CONTEXT, ADR 0090); the prompt states the same kind in a word no 档 claims.
AGENT_IDENTITY: str = (
    "fin — enterprise knowledge-QA agent on the operator "
    "工作台: the 入口, not the 库. "
    "Each turn is a track (dialogue / clarify / off_topic / grounded); "
    "tools and the citation verifier bind only to the grounded track. "
    "The 知识库 is the one corpus every research thread shares."
)

AGENT_SOUL: str = (
    "Speak plainly. Ground every claim about the corpus in this run's "
    "tool output; do not present parametric memory as evidence — numbers "
    "and document claims need retrieve or read_source text. Refuse to "
    "fabricate; avoid invented company names, dates or periods, and "
    "made-up document ids."
)

# ----- the no-evidence refusal (spec demo-seed item 6 / ADR 0100 D6) --
#
# What the answer says when the corpus does not support the question.
# ONE definition, here, because the string is user-facing: two copies
# drift and only the one the model saw would be wrong.
#
# WHY the instruction and NOT a backend envelope: the backend has no
# surface that renders answer prose for this case. The citation gate's
# ``RefuseResult`` is a run status and the evidence contract's
# ``EvidenceRefusal`` is a machine code for a structurally invalid
# producer payload — both stop or reject, neither writes an answer. A
# grounded turn whose tools returned no supporting text is still a
# turn that must answer, so the sentence has to come from the model
# instruction, and the gate keeps doing the part that is mechanical:
# an answer with no grounded span never becomes ``done``.
NO_EVIDENCE_REFUSAL: str = "资料中未找到相关内容"


# ----- per-track usage text (CONTEXT *usage*; tool-choice + citation
# behavior per track) -------------------------------------------------
# Research usage names every research tool and describes citation
# behavior without restating the verifier's numeric caps (CONTEXT
# *usage* _Avoid_: no page_limit cap, no 32-character rule). The
# dialogue / off_topic / clarify tracks share identity + soul and
# differ only in usage text.
USAGE_RESEARCH: str = (
    "Use the research tools before you answer; do not rely on what "
    "you already know about the corpus or its subject. The 知识库 holds "
    "ordinary enterprise material — policies, handbooks, product guides, "
    "meeting notes, reports, contracts, structured records — so any "
    "question about it is a normal research question.\n"
    "- list_files: catalog over the 知识库. Optional filters narrow it; "
    "read_source needs document_id and the version (sha256) the catalog "
    "returns.\n"
    "- retrieve: the retrieve port's search over the ingested corpus on "
    "the memory service (passage chunks over frozen units). 查已入库 goes "
    "through retrieve — prefer it to locate a figure or sentence; pass "
    "document_id when you know the document.\n"
    "- read_source: one bounded slice of a frozen unit. source_ref is a "
    "document locator {document_id, version, unit} or an attachment "
    "locator {kind: 'attachment', attachment_id, version, unit}; take the "
    "version from tool output this run, never invent it; unit is 1-based. "
    "position is a zero-based code-point offset into that unit; continue at "
    "next_position until it is null. A known locator needs no retrieve; "
    "only text a read returned is citable.\n"
    "- get_xbrl_fact: one tagged XBRL fact from a filing's fact table "
    "(concept + reporting period; document_id narrows to one filing) — the "
    "tool for financial filings, one source category among others. A miss "
    "returns a typed not-found result; the fact row carries its own "
    "source_locator — read that unit for a citable span.\n"
    "\n"
    "Finish only after a tool this run returned unit or chunk text. "
    "Copy a sentence, table row, or caption verbatim into the answer "
    "as a citation span; do not paraphrase the span, and do not "
    "invent document_id values. Then write ordinary prose including "
    "those copied spans and stop calling tools.\n"
    "\n"
    "When the ask has two genuinely different readings and this run "
    f"returned more than one document that could answer it, ask which: "
    f"one question sentence, then {CANDIDATE_TERMINATOR} on its own line, "
    f'then one JSON object {{"{CANDIDATE_PAYLOAD_FIELD}": '
    '[{"document_id": "..."}]} naming at most four document_ids — or '
    "attachment_ids for attached files — that a tool returned this run. "
    "An answer drawing on several documents is ordinary prose with its "
    "citations.\n"
    "\n"
    "When no tool this run returned text supports the question, do not "
    f"fill the gap from memory: say {NO_EVIDENCE_REFUSAL} "
    "plainly, add nothing else, and cite nothing. That sentence is the "
    "whole answer — an estimate, a nearby-period number, or a "
    "plausible-looking document id would be fabrication.\n"
)

USAGE_DIALOGUE: str = (
    "Respond warmly and briefly to greetings and small-talk. Do not "
    "invent corpus facts; do not claim you retrieved documents. "
    "Invite the operator to ask about a document, a topic, or a figure "
    "when it fits the conversation."
)

USAGE_OFF_TOPIC: str = (
    "The operator's ask is outside fin's scope. Reply with a soft, "
    "polite capability-bound refusal in ordinary prose — do not "
    "invent corpus facts, and do not use a fixed redirect string as "
    "the only reply. Briefly name what fin can help with instead."
)

USAGE_CLARIFY: str = (
    "The operator asked about material the 知识库 may not hold, but a "
    "catalog slot is still missing. Ask briefly which slots are still "
    "needed and ground the options in the 知识库 via list_files rather "
    "than a frozen snapshot. Do not invent company names, dates or "
    "periods, or document identifiers; do not claim you retrieved "
    "documents."
)

# Assembly: each track prompt is the per-track marker line, then the
# shared identity, then the shared soul, then the per-track usage.
# Markers stay first (idempotence in ``messages_for_model`` keys on
# them). ``HOST_SYSTEM_PROMPT`` keeps its name so existing tests that
# import it continue to compile; only its value changes.
HOST_SYSTEM_PROMPT: str = (
    f"{HOST_INSTRUCTIONS_MARKER}\n"
    f"{AGENT_IDENTITY}\n"
    f"{AGENT_SOUL}\n"
    f"{USAGE_RESEARCH}"
)

DIALOGUE_SYSTEM_PROMPT: str = (
    f"{DIALOGUE_INSTRUCTIONS_MARKER}\n"
    f"{AGENT_IDENTITY}\n"
    f"{AGENT_SOUL}\n"
    f"{USAGE_DIALOGUE}"
)

OFF_TOPIC_SYSTEM_PROMPT: str = (
    f"{OFF_TOPIC_INSTRUCTIONS_MARKER}\n"
    f"{AGENT_IDENTITY}\n"
    f"{AGENT_SOUL}\n"
    f"{USAGE_OFF_TOPIC}"
)

CLARIFY_SYSTEM_PROMPT: str = (
    f"{CLARIFY_INSTRUCTIONS_MARKER}\n"
    f"{AGENT_IDENTITY}\n"
    f"{AGENT_SOUL}\n"
    f"{USAGE_CLARIFY}"
)

# ----- per-档 usage copy (search-mode host T3 / ADR 0061) ------------
# 检索档 is NOT a fifth track: the copy rides INSIDE the head
# SystemMessage after the track prompt, added at model-call time when
# this turn's cascade ASSIGNED a 档 (rules / llm / inherit /
# operator_pick / the recorded ``fallback`` default). An unassigned turn
# (a typed fail) carries no copy. Like every block here the copy is
# call-scoped only — checkpointed history is never rewritten when the mode
# changes, and after session compaction the next model call re-adds the
# copy (ADR 0061).
#
# The copy describes the path the host chose and NEVER names it. The mode
# is not the model's business (ADR 0104): the host assigns it and the
# retrieve call carries it, so a copy that named a mode taught the model a
# vocabulary it must not use and an argument it cannot send. The model
# then narrated the mode in its own reasoning — and that reasoning is
# rendered — and spent turns trying to send a filter the tool schema does
# not accept. A reader that needs the mode reads the turn receipt.
SEARCH_MODE_INSTRUCTIONS_MARKER: str = "[FIN_SEARCH_MODE]"

# One body per 档, bound by its lasting token: one 档 means one text, so
# the model is never shown two texts for it. The KEY is the mode's own
# token; the TEXT names no mode at all.
_USAGE_LEXICAL: str = (
    f"{SEARCH_MODE_INSTRUCTIONS_MARKER}\n"
    "This turn's retrieval answers with keyword-first matches over "
    "the corpus. Query retrieve with the operator's own keywords, "
    "open a hit with read_source before quoting it. If the exact "
    "keywords miss, rephrase them once, then tell the operator "
    "plainly that the 资料 has no keyword hit."
)

_USAGE_KNOWLEDGE_QA: str = (
    f"{SEARCH_MODE_INSTRUCTIONS_MARKER}\n"
    "This turn's retrieval walks the entity and relation "
    "neighbourhoods first, then fills with vector "
    "and BM25 passages. Ask retrieve the question as phrased; "
    "quote only unit text this run returned — relation edges "
    "are not citable evidence."
)

_USAGE_REVIEW: str = (
    f"{SEARCH_MODE_INSTRUCTIONS_MARKER}\n"
    "This turn's retrieval draws on "
    "community summaries and report text prepared at index "
    "time, then drops to windows for exact spans when needed. "
    "Assemble the answer from report text this run returned "
    "and keep quoted spans verbatim."
)

_USAGE_REAL_DB: str = (
    f"{SEARCH_MODE_INSTRUCTIONS_MARKER}\n"
    "This turn's retrieval targets business data held in a live "
    "database, not the document corpus. Bound the query with the "
    "tool's expiry-window arguments when the operator stated a "
    "window; when that path is not wired, report the named failure "
    "the tool returned."
)

# Keyed by the lasting tokens alone. Every 档 is named by the word form of
# the arm that answers under it, so there is no second spelling to resolve:
# a retired letter is out of the closed set and normalizes to nothing, which
# is why no legacy key ever had to be added here.
SEARCH_MODE_USAGE_COPY: dict[str, str] = {
    "lexical": _USAGE_LEXICAL,
    "graph": _USAGE_KNOWLEDGE_QA,
    "global": _USAGE_REVIEW,
    "sql": _USAGE_REAL_DB,
}


def _usage_copy_for(search_mode: object) -> str | None:
    """The 档 copy for THIS turn's token, or ``None`` for no copy.

    Spelling→token resolution is delegated to the SSOT's
    :func:`normalize_tier_token` — one authority for casing and for the
    closed set, instead of a second case-folding rule beside this dict.
    Anything outside the accepted spellings resolves to nothing,
    never to the nearest 档; a lasting token with no copy (an unwired 档)
    likewise injects nothing rather than another 档's instructions.
    """
    token = normalize_tier_token(search_mode)
    if token is None:
        return None
    return SEARCH_MODE_USAGE_COPY.get(token)


# Operator-facing ask per slot the clarify track can be missing.
# A slot absent from this map is never named in the prompt: the
# corpus only has company and period as routing slots, and an
# unknown label would put a term we cannot ground in front of the
# operator. Asks reference catalog fields only — no ticker or
# quarter hardcodes (spec SC10 / SC11): the actual values come from
# ``list_files`` at model call time, not from a frozen snapshot.
_CLARIFY_SLOT_ASKS: dict[str, str] = {
    "company": "which company appears in the 知识库",
    "period": "which reporting period the 知识库 covers",
}


def clarify_prompt(missing_slots: Sequence[str] | None = None) -> str:
    """Return the clarify prompt naming THIS turn's missing slots.

    ``missing_slots`` comes from the turn's track decision or the
    latest status snapshot. With no recognized slot the static
    ``CLARIFY_SYSTEM_PROMPT`` stands — a snapshot's dependent
    fields may legitimately be empty (CONTEXT *status snapshot*),
    and a guess about which slot is missing is worse than the
    generic ask.
    """
    named = [
        slot
        for slot in dict.fromkeys(missing_slots or ())
        if slot in _CLARIFY_SLOT_ASKS
    ]
    if not named:
        return CLARIFY_SYSTEM_PROMPT
    asks = " and ".join(_CLARIFY_SLOT_ASKS[slot] for slot in named)
    return (
        f"{CLARIFY_SYSTEM_PROMPT}\n"
        f"Still missing on this turn: {', '.join(named)}. Ask for "
        f"{asks} — and do not re-ask for anything the operator "
        "already gave."
    )


def attachment_context_message(
    attachments: Sequence[dict[str, Any]] | None,
) -> SystemMessage | None:
    """Return the call-scoped attachment block, or ``None`` for none.

    corpus-serve-ingest T4 (ADR 0047 **thread attach**): the operator
    may drag a file into the open research thread, and the thread keeps
    it for its whole life (ADR 0086). The block itself is still
    call-scoped: like the track prompts, the returned ``SystemMessage``
    is prepended at model call time and must never be written back to
    the checkpoint, because the text is re-stuffed from the thread's
    stored copy on every run that names the attachment. A later run
    that does NOT name it gets no block, so the text is absent for that
    run — the attachment persists, its in-context text does not.

    Citation discipline (ADR 0046): the block says explicitly that
    attachments are NOT citable corpus. The gate's real protection is
    structural, not prompt-level — attachment text never enters the
    ``evidence`` channel, so an attachment-only span cannot match.

    G4 T3 (thread-attachment-source-read decision 7): the block header
    also names the file's ``attachment_id`` next to its filename, because
    the ``retrieve`` tool's ``attachment_ids`` argument accepts ids only —
    a model that wants to search one specific attachment has to be able to
    say which one it means, and the filename alone cannot name it
    deterministically (two files can share a name; an id cannot).
    """
    if not attachments:
        return None
    sections = []
    for attachment in attachments:
        filename = str(attachment.get("filename") or "attachment")
        attachment_id = str(attachment.get("attachment_id") or "")
        text = str(attachment.get("text") or "")
        # The id rides in brackets AFTER the filename so the header's
        # leading ``attached file: {filename}`` shape — which the
        # preparation-gate suite reads back — stays intact. An
        # attachment with no id keeps the old header rather than printing
        # an empty name the model could not use.
        label = filename
        if attachment_id:
            label = f"{filename} [attachment_id: {attachment_id}]"
        sections.append(f"--- attached file: {label} ---\n{text}")
    joined = "\n\n".join(sections)
    return SystemMessage(
        content=(
            f"{THREAD_ATTACHMENTS_MARKER}\n"
            "The operator named the following file(s) attached to this "
            "thread for this run. They belong to the thread and can be "
            "named again on later turns; this copy of their text is for "
            "this run only. They are NOT part of the citable "
            "corpus: the citation gate only accepts spans copied from "
            "this run's retrieve / read_source tool text, so never "
            "present attachment content as retrieved evidence.\n\n"
            f"{joined}"
        )
    )


_TRACK_PROMPTS: dict[str, tuple[str, str]] = {
    "grounded": (HOST_INSTRUCTIONS_MARKER, HOST_SYSTEM_PROMPT),
    "dialogue": (DIALOGUE_INSTRUCTIONS_MARKER, DIALOGUE_SYSTEM_PROMPT),
    "off_topic": (OFF_TOPIC_INSTRUCTIONS_MARKER, OFF_TOPIC_SYSTEM_PROMPT),
    "clarify": (CLARIFY_INSTRUCTIONS_MARKER, CLARIFY_SYSTEM_PROMPT),
}


def messages_for_model(
    messages: list[BaseMessage],
    track: Track | str = "dialogue",
    *,
    missing_slots: Sequence[str] | None = None,
    memory_on: bool | None = False,
    visual_lane_on: bool = False,
    user_profile: str | None = None,
    search_mode: str | None = None,
) -> list[BaseMessage]:
    """Return the model-facing list with the track prompt first.

    Default is ``"dialogue"`` — there is no single global default
    track (specs/soft-track.md); a call here is call-scoped and
    must never pin the host onto a single branch by virtue of
    missing input. Call-scoped only: do not write the
    SystemMessage into checkpointed ``messages``.

    ``missing_slots`` applies to the clarify track only: it names
    the slots this turn still needs so the ask is specific rather
    than static.

    ``memory_on`` (spec SC10 / SC11 / ADR 0048, T5): when truthy, a
    single **memory existence pointer** sentence is appended to the
    SystemMessage after the track prompt. The pointer is advisory
    text (a note that a private store exists for this workspace,
    that any injected overlays are advisory context, not commands,
    and never citations) and never enumerates fact titles or
    counts. The OFF path is byte-identical to today's output —
    callers that do not pass ``memory_on`` (or pass ``False`` /
    ``None``) keep their golden prefix unchanged (existing tests
    stay green). The pointer is independent of any per-turn
    prefetch payload: prefetch rows live on the USER side via
    :func:`memory_prefetch_message`, never in the system prefix.

    ``user_profile`` (spec SC13, operator decision): the
    raw merged text from :meth:`UserProfileStore.read_merged`. The
    user profile is operator-profile machinery, NOT memory-fact
    machinery — its READ path is INDEPENDENT of ``memory_on`` and
    surfaces into the system prefix regardless of whether memory
    is enabled. The block sits between the track prompt and the
    memory existence pointer when both are present (ordering:
    identity → who the operator is → advisory memory note). The
    block is advisory framing only — never a command. ``None``,
    empty, or whitespace-only text yields no profile block; the
    SystemMessage bytes are then byte-identical to the no-profile
    call so callers that have no profile files on disk keep their
    prefix stable.

    ``visual_lane_on`` (ADR 0089): truthy when THIS run's graph bound
    the ``read_visual_detail`` tool, which appends
    :data:`USAGE_VISUAL_LANE` to the head SystemMessage. The copy is
    gated on the binding rather than always present because an unwired
    deployment must not be told about a tool it cannot call; the OFF path
    is byte-identical to the call without it.

    ``search_mode`` (search-mode host T3 / ADR 0061): the 检索档
    this turn's cascade ASSIGNED (one of ``LASTING_RETRIEVE_TIERS``).
    When set and
    recognized, that 档's usage copy is appended to the head
    SystemMessage after the blocks above — call-scoped, exactly
    once, idempotent on re-entry. ``None`` / empty / an out-of-set
    token (clarify awaiting a pick, typed fail, non-grounded
    tracks) injects nothing: the bytes are then identical to the
    pre-T3 call. Compaction re-inject needs no extra seam: history
    rebuild never touches this call-time head, so the next model
    call re-adds the copy by construction.
    """
    resolved = normalize_track(track) or "dialogue"
    marker, prompt = _TRACK_PROMPTS.get(
        resolved, (DIALOGUE_INSTRUCTIONS_MARKER, DIALOGUE_SYSTEM_PROMPT)
    )
    if resolved == "clarify":
        prompt = clarify_prompt(missing_slots)
    if (
        messages
        and isinstance(messages[0], SystemMessage)
        and marker in str(messages[0].content)
    ):
        existing = list(messages)
    else:
        existing = [SystemMessage(content=prompt), *messages]

    # Profile block: independent of memory_on. Inserted BETWEEN the
    # track prompt and the memory existence pointer when both are
    # present. ``None`` / empty / whitespace-only → no block
    # (byte-identical output for callers with no profile files).
    profile_text = user_profile.strip() if isinstance(user_profile, str) else ""
    if profile_text:
        existing = _inject_profile_block(existing, profile_text)

    if not memory_on:
        # OFF path: byte-identical to today's output for callers that
        # do not opt in. The track prompt + tail of messages is exactly
        # what messages_for_model produced before T5.
        result = existing
    else:
        result = _append_memory_pointer(existing)

    if visual_lane_on:
        result = _append_block_to_head(
            result, VISUAL_LANE_INSTRUCTIONS_MARKER, USAGE_VISUAL_LANE
        )

    mode_block = _usage_copy_for(search_mode)
    if mode_block is None:
        return result
    return _append_block_to_head(
        result, SEARCH_MODE_INSTRUCTIONS_MARKER, mode_block
    )


def _append_memory_pointer(messages: list[BaseMessage]) -> list[BaseMessage]:
    """Append the memory existence pointer to the head SystemMessage.

    Split out of :func:`messages_for_model` (T3) — behaviour is the
    T5 ON path verbatim: a non-SystemMessage head or an already
    present pointer leaves the payload unchanged (idempotent).
    """
    head = messages[0]
    if not isinstance(head, SystemMessage):
        # Defensive: a non-SystemMessage head is not the expected shape,
        # but we must not crash a model call over a pointer that the
        # spec marks advisory. Skip silently rather than inject a
        # second SystemMessage (which would break call-scoped-only).
        return messages
    head_content = str(head.content)
    if MEMORY_POINTER_MARKER in head_content:
        # Pointer already present (caller pre-injected it, or this is
        # a re-entry): leave the message unchanged. Idempotent.
        return messages
    rebuilt = list(messages)
    rebuilt[0] = SystemMessage(content=f"{head_content}\n\n{MEMORY_POINTER_BLOCK}")
    return rebuilt


def _append_block_to_head(
    messages: list[BaseMessage], block_marker: str, block_text: str
) -> list[BaseMessage]:
    """Append a marker-prefixed advisory block to the head SystemMessage.

    Same idempotence / never-crash shape as :func:`_inject_profile_block`:
    the block rides the existing head message (one SystemMessage, no
    stacking on re-entry); a non-SystemMessage or empty payload gets a
    fresh head rather than a crashed model call.
    """
    if not messages:
        return [SystemMessage(content=block_text)]
    head = messages[0]
    if not isinstance(head, SystemMessage):
        return [SystemMessage(content=block_text), *messages]
    if block_marker in str(head.content):
        return messages
    rebuilt = list(messages)
    rebuilt[0] = SystemMessage(
        content=f"{str(head.content)}\n\n{block_text}"
    )
    return rebuilt


def _inject_profile_block(
    messages: list[BaseMessage], profile_text: str
) -> list[BaseMessage]:
    """Insert the user-profile block into the head SystemMessage.

    Idempotent: if the head already carries the profile marker
    (re-entrant call, caller pre-injected), the block is left
    untouched. The block is built fresh from ``profile_text`` so a
    file edit between calls is visible on the next ``messages_for_model``
    call (no caching at this seam). Whitespace-only input is the
    caller's responsibility — this helper assumes a non-empty
    ``profile_text`` because the caller filters ``None`` / ``""`` /
    whitespace-only strings before invoking.
    """
    if not messages:
        return [SystemMessage(content=_build_profile_block(profile_text))]
    head = messages[0]
    if not isinstance(head, SystemMessage):
        # Defensive: non-SystemMessage head (unexpected shape). Drop
        # a fresh SystemMessage in front so the model still sees the
        # block; never crash a model call over an advisory block.
        return [
            SystemMessage(content=_build_profile_block(profile_text)),
            *messages,
        ]
    head_content = str(head.content)
    if USER_PROFILE_MARKER in head_content:
        # Block already present (re-entry): leave the message unchanged
        # so a second call with the same text doesn't shift bytes.
        return messages
    new_content = (
        f"{head_content}\n\n{_build_profile_block(profile_text)}"
    )
    rebuilt = list(messages)
    rebuilt[0] = SystemMessage(content=new_content)
    return rebuilt


def _build_profile_block(profile_text: str) -> str:
    """Render the profile block as a marker-prefixed string.

    One advisory header line naming the workspace-overlay semantics,
    then the verbatim file content. Header wording follows
    ``docs/guides/prompt-development.md``: framing only, never a
    command to the model.
    """
    return f"{USER_PROFILE_MARKER}\n{USER_PROFILE_HEADER}\n{profile_text}"


def memory_prefetch_message(
    prefetch_rows: Sequence[Mapping[str, Any]] | None,
) -> HumanMessage | None:
    """Render the per-turn prefetch rows as a USER-side HumanMessage.

    ``prefetch_rows`` is the list produced by
    :func:`fin.host.memory_prefetch.select_prefetch` — each row is a
    mapping carrying ``fact_id``, ``title``, ``hook``. The block
    surfaces ``title + hook`` per row and is prefixed with the
    :data:`FIN_MEMORY_PREFETCH_MARKER` so other layers can locate
    it. ``body`` MUST NOT flow into the block (the prefetch payload
    is title+hook only — ``body`` bytes are accounted by the
    occupancy ledger but never reach the model).

    Call-scoped only: like the system-prompt injection, this message
    is appended to the model-facing payload for THIS turn only and
    must NEVER be written back to the checkpoint. The caller drops
    it after the call.

    Returns ``None`` when there are no rows (zero lexical hits
    surface nothing, per spec SC7). The caller can then skip the
    append entirely — adding an empty marker block would change the
    token count and break the "zero hits inject nothing" rule.
    """
    if not prefetch_rows:
        return None

    lines: list[str] = []
    for row in prefetch_rows:
        if not isinstance(row, Mapping):
            # Skip a malformed row rather than crashing the model call;
            # the assembler contract is "best-effort advisory context",
            # not a hard gate (the gate on prefetch content lives in
            # the citation verifier, which never sees this block).
            continue
        fact_id = str(row.get("fact_id", "")).strip()
        title = str(row.get("title", "")).strip()
        hook = str(row.get("hook", "")).strip()
        if not title and not hook:
            continue
        # Single readable line per row: "<title> — <hook>" with the
        # fact_id as a parenthetical anchor so a downstream tool call
        # can resolve to the source row. ``body`` is intentionally
        # NEVER read here — see docstring.
        head = f"- {title}" if title else "- (untitled)"
        if hook:
            head = f"{head} — {hook}"
        if fact_id:
            head = f"{head} ({fact_id})"
        lines.append(head)

    if not lines:
        return None

    body = "\n".join(lines)
    content = (
        f"{FIN_MEMORY_PREFETCH_MARKER}\n"
        "The following notes from your private workspace memory "
        "matched this turn lexically. They are advisory context "
        "for this turn only and never count as evidence or "
        "citations.\n"
        f"{body}"
    )
    return HumanMessage(content=content)


# Architecture alias used by Soft-Track leaves.
messages_for_track = messages_for_model


__all__ = [
    "AGENT_IDENTITY",
    "AGENT_SOUL",
    "CLARIFY_INSTRUCTIONS_MARKER",
    "CLARIFY_SYSTEM_PROMPT",
    "DIALOGUE_INSTRUCTIONS_MARKER",
    "DIALOGUE_SYSTEM_PROMPT",
    "FIN_MEMORY_PREFETCH_MARKER",
    "HOST_INSTRUCTIONS_MARKER",
    "HOST_SYSTEM_PROMPT",
    "MEMORY_POINTER_BLOCK",
    "MEMORY_POINTER_MARKER",
    "MEMORY_POINTER_TEXT",
    "OFF_TOPIC_INSTRUCTIONS_MARKER",
    "OFF_TOPIC_SYSTEM_PROMPT",
    "SEARCH_MODE_INSTRUCTIONS_MARKER",
    "SEARCH_MODE_USAGE_COPY",
    "THREAD_ATTACHMENTS_MARKER",
    "USAGE_CLARIFY",
    "USAGE_DIALOGUE",
    "USAGE_OFF_TOPIC",
    "USAGE_RESEARCH",
    "USAGE_VISUAL_LANE",
    "USER_PROFILE_HEADER",
    "USER_PROFILE_MARKER",
    "VISUAL_LANE_INSTRUCTIONS_MARKER",
    "attachment_context_message",
    "clarify_prompt",
    "memory_prefetch_message",
    "messages_for_model",
    "messages_for_track",
]
