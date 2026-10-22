"""Frozen value types and vocabulary for the question and task runtime.

CONTEXT **host agent**: fin as the LangGraph runtime that routes tracks
and calls grounded-track tools behind the **retrieve port**. These are
the values that runtime stores on the authoritative ``AgentState`` and
hands to the peer modules; the decisions that move them live in
:mod:`fin.host.task.task_runtime`.

Split from the decision module by responsibility, not by convenience:
every name here is DATA (a vocabulary token, a frozen record, or a
total-function codec between a record and its stored dict), while
``task_runtime`` holds only the transitions. Keeping them apart means a
surface can decode a stored envelope without also being able to change
one.

Three facts the specs keep separate, and where each lives here:

* **accepted input** — an original user payload plus its provenance, and
  the integer ``input_version`` it advanced to. Every accepted message,
  valid clarification reply, and explicit restore request advances input
  history exactly once.
* **task version** — an integer that moves only when the goal, expected
  result, or necessary conditions materially change.
* **task outcome vs turn outcome** — a turn may end while its task stays
  active, so the two are separate records and never inferred from each
  other.

Shape SSOT is ``fin.host.task.task_runtime_types``. The wire spelling is
frozen in ``specs/question-task-runtime-contract.md``.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Final, Literal, Mapping

# ----- the frozen vocabulary -----

INPUT_MESSAGE: Final[str] = "message"
INPUT_REPLY: Final[str] = "reply"
INPUT_RESTORE: Final[str] = "restore"
INPUT_KINDS: Final[frozenset[str]] = frozenset(
    {INPUT_MESSAGE, INPUT_REPLY, INPUT_RESTORE}
)

# What dependent work a committed transition authorises. ``none`` is the
# taskless disposition: a greeting or off-topic turn reconciles and then
# starts nothing, so it can never grow a task outcome by accident.
DISPOSITION_NONE: Final[str] = "none"
DISPOSITION_START: Final[str] = "start"
DISPOSITION_CONTINUE: Final[str] = "continue"
DISPOSITION_SWITCH: Final[str] = "switch"
DISPOSITION_RESTORE: Final[str] = "restore"
DISPOSITION_AWAIT_REPLY: Final[str] = "await_reply"
DISPOSITION_CANCEL: Final[str] = "cancel"
EXECUTION_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        DISPOSITION_NONE,
        DISPOSITION_START,
        DISPOSITION_CONTINUE,
        DISPOSITION_SWITCH,
        DISPOSITION_RESTORE,
        DISPOSITION_AWAIT_REPLY,
        DISPOSITION_CANCEL,
    }
)

TASK_ACTIVE: Final[str] = "active"
TASK_COMPLETE: Final[str] = "complete"
TASK_CANCELLED: Final[str] = "cancelled"
TASK_OUTCOMES: Final[frozenset[str]] = frozenset(
    {TASK_ACTIVE, TASK_COMPLETE, TASK_CANCELLED}
)

# The exactly-one Other choice. Its id is a reserved token rather than a
# generated one, so a reply is recognised as free text without guessing
# which option it picked.
OTHER_CHOICE_ID: Final[str] = "other"

# WHY a pending question, not only a task clarification: a genuinely
# ambiguous ask offers admitted documents, and those candidates carry
# identity and version facts a clarification option never had. The
# purpose token keeps the two apart so an existing question's stored
# meaning is unchanged by the wider shape.
PURPOSE_TASK_CLARIFICATION: Final[str] = "task_clarification"
PURPOSE_DOCUMENT_CHOICE: Final[str] = "document_choice"
DOCUMENT_CHOICE_PURPOSES: Final[frozenset[str]] = frozenset(
    {PURPOSE_TASK_CLARIFICATION, PURPOSE_DOCUMENT_CHOICE}
)

# The closed source vocabulary a choice's reference may name. Anything
# else is a kind no admission path can have checked, so it is refused
# rather than stored.
SOURCE_KIND_CORPUS: Final[str] = "corpus"
SOURCE_KIND_ATTACHMENT: Final[str] = "attachment"
SOURCE_KINDS: Final[frozenset[str]] = frozenset(
    {SOURCE_KIND_CORPUS, SOURCE_KIND_ATTACHMENT}
)

# The shared "at most four readable candidates" bound. One definition, so
# the projection, the admission check and the presentation cannot each
# pick their own number. The trailing Other is NOT one of these: it is
# added by ``offered_choices``.
MAX_DOCUMENT_CHOICES: Final[int] = 4

# Per-field ceilings in UTF-8 bytes. A proposal over one is refused
# whole rather than shortened, because a truncated label or summary drops
# the meaning that made the candidate worth choosing. These apply to NEW
# document choices only and never retroactively to existing questions.
DOCUMENT_CHOICE_ID_BYTES: Final[int] = 256
DOCUMENT_CHOICE_LABEL_BYTES: Final[int] = 512
DOCUMENT_CHOICE_SUMMARY_BYTES: Final[int] = 1024
DOCUMENT_CHOICE_PROMPT_BYTES: Final[int] = 8192

# The three fields whose material change moves the task version.
CHANGE_GOAL: Final[str] = "goal"
CHANGE_EXPECTED_RESULT: Final[str] = "expected_result"
CHANGE_NECESSARY_CONDITIONS: Final[str] = "necessary_conditions"
MATERIAL_CHANGE_KINDS: Final[frozenset[str]] = frozenset(
    {CHANGE_GOAL, CHANGE_EXPECTED_RESULT, CHANGE_NECESSARY_CONDITIONS}
)

# Bounded read surfaces. Overflow drops the OLDEST entry, and the read
# reports the bound rather than pretending the history is complete — a
# missing original stays unknown instead of being reconstructed.
INPUT_HISTORY_LIMIT: Final[int] = 200
ARCHIVE_LIMIT: Final[int] = 50


# ----- named failures -----

TASK_INPUT_INVALID: Final[str] = "task_input_invalid"
TASK_INPUT_UNRECONCILED: Final[str] = "task_input_unreconciled"
TASK_PERMISSION_DENIED: Final[str] = "task_permission_denied"
TASK_DEPENDENCY_FAILURE: Final[str] = "task_dependency_failure"
TASK_CAPABILITY_UNAVAILABLE: Final[str] = "task_capability_unavailable"
TASK_CAPABILITY_UNSUPPORTED: Final[str] = "task_capability_unsupported"
TASK_INSUFFICIENT_EVIDENCE: Final[str] = "task_insufficient_evidence"
TASK_APPLICABILITY_UNRESOLVED: Final[str] = "task_applicability_unresolved"
TASK_INTENT_MISSING: Final[str] = "task_intent_missing"
TASK_EXECUTION_LIMIT: Final[str] = "task_execution_limit"
TASK_CHECKPOINT_FAILED: Final[str] = "task_checkpoint_failed"
TASK_HISTORY_FAILED: Final[str] = "task_history_failed"
TASK_REPLY_EXPIRED: Final[str] = "task_reply_expired"
TASK_REPLY_UNKNOWN: Final[str] = "task_reply_unknown"
TASK_RESTORE_UNKNOWN: Final[str] = "task_restore_unknown"
TASK_RESTORE_NOT_ADOPTED: Final[str] = "task_restore_not_adopted"
TASK_ALREADY_RECONCILED: Final[str] = "task_already_reconciled"

TASK_FAILURE_KINDS: Final[frozenset[str]] = frozenset(
    {
        TASK_INPUT_INVALID,
        TASK_INPUT_UNRECONCILED,
        TASK_PERMISSION_DENIED,
        TASK_DEPENDENCY_FAILURE,
        TASK_CAPABILITY_UNAVAILABLE,
        TASK_CAPABILITY_UNSUPPORTED,
        TASK_INSUFFICIENT_EVIDENCE,
        TASK_APPLICABILITY_UNRESOLVED,
        TASK_INTENT_MISSING,
        TASK_EXECUTION_LIMIT,
        TASK_CHECKPOINT_FAILED,
        TASK_HISTORY_FAILED,
        TASK_REPLY_EXPIRED,
        TASK_REPLY_UNKNOWN,
        TASK_RESTORE_UNKNOWN,
        TASK_RESTORE_NOT_ADOPTED,
        TASK_ALREADY_RECONCILED,
    }
)



class TaskRuntimeError(Exception):
    """A typed task failure.

    ``kind`` is the wire token from :data:`TASK_FAILURE_KINDS`; callers
    branch on it and never on the message, which is for operators and
    logs only. ``detail`` carries the machine-readable facts behind the
    refusal (which question expired, which archive was asked for) so a
    surface reports WHICH failure without this module inventing per-call
    copy.
    """

    def __init__(self, kind: str, message: str, **detail: Any) -> None:
        if kind not in TASK_FAILURE_KINDS:  # pragma: no cover - programmer error
            raise AssertionError(f"undeclared failure kind: {kind}")
        super().__init__(message)
        self.kind = kind
        self.detail: dict[str, Any] = dict(detail)



# ----- value objects -----


@dataclass(frozen=True)
class AcceptedInput:
    """One durably accepted input, recorded before any semantic work.

    ``payload`` is the ORIGINAL text as submitted — never a paraphrase,
    never a summary. ``provenance`` names where it came from (the send
    door, the clarification reply door, the restore door) so a later
    reader can tell a user-authored payload from a host-authored one.
    ``payload is None`` means the original is genuinely unavailable; a
    summary or assistant prose must never stand in for it.
    """

    input_version: int
    kind: str
    payload: str | None
    provenance: str
    question_id: str | None = None
    archive_id: str | None = None
    choice_id: str | None = None
    run_id: str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "input_version": self.input_version,
            "kind": self.kind,
            "payload": self.payload,
            "provenance": self.provenance,
            "question_id": self.question_id,
            "archive_id": self.archive_id,
            "choice_id": self.choice_id,
            "run_id": self.run_id,
        }


@dataclass(frozen=True)
class TaskState:
    """The current task: its goal, its version, and its outcome.

    ``goal`` / ``expected_result`` / ``necessary_conditions`` are the
    three things whose material change moves ``task_version``. They stay
    plain comparable values so "did this input materially change the
    task?" is decidable rather than a judgement call.
    """

    task_id: str
    goal: str
    expected_result: str
    task_version: int = 1
    necessary_conditions: tuple[str, ...] = ()
    outcome: str = TASK_ACTIVE
    outcome_reason: str = ""
    created_at_input_version: int = 1

    def __post_init__(self) -> None:
        if self.outcome not in TASK_OUTCOMES:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                f"unknown task outcome: {self.outcome}",
                task_id=self.task_id,
            )

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "expected_result": self.expected_result,
            "task_version": self.task_version,
            "necessary_conditions": list(self.necessary_conditions),
            "outcome": self.outcome,
            "outcome_reason": self.outcome_reason,
            "created_at_input_version": self.created_at_input_version,
        }


@dataclass(frozen=True)
class ArchivedTask:
    """A switched-away task, kept locatable with its references.

    ``evidence_refs`` and ``run_id`` are what a later restore has to
    revalidate: an archived result stays historical until a CURRENT
    assessment supports adopting it.
    """

    archive_id: str
    task: TaskState
    archived_at_input_version: int
    run_id: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "archive_id": self.archive_id,
            "task": self.task.to_state_dict(),
            "archived_at_input_version": self.archived_at_input_version,
            "run_id": self.run_id,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True)
class ChoiceSourceRef:
    """The admitted hit one document choice points at.

    ``version`` is stored beside the id because a saved source choice is
    user INTENT: it is what the user picked, and a later read has to
    recheck it against the current source identity rather than assume the
    saved version is still current. ``section`` narrows the reference to
    one part of a source and is ``None`` when the whole source was
    admitted.
    """

    source_kind: Literal["corpus", "attachment"]
    source_id: str
    version: str
    section: str | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "version": self.version,
            "section": self.section,
        }


@dataclass(frozen=True)
class ChoiceDetail:
    """One candidate's presentation metadata and its admitted reference.

    ``label`` and ``summary`` are what the user reads; they are
    presentation metadata, never citation evidence and never stored
    permission. The evidence is rechecked at read time through
    ``source_ref``.
    """

    choice_id: str
    label: str
    summary: str
    source_ref: ChoiceSourceRef

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "choice_id": self.choice_id,
            "label": self.label,
            "summary": self.summary,
            "source_ref": self.source_ref.to_state_dict(),
        }


@dataclass(frozen=True)
class PendingQuestion:
    """A clarification awaiting a reply.

    The full prompt and every listed choice are preserved as authored,
    and the choice list ends with exactly one Other. ``task_id`` is the
    task the question is BOUND to — a question created during a task
    transition binds to the resulting task, never to a previous or null
    task.

    ``purpose`` separates the two kinds this one record carries. A
    ``task_clarification`` — every question asked before the document
    choice existed — carries no choice details and needs no originating
    run, so the wider record decodes and re-encodes it unchanged. A
    ``document_choice`` names the run it interrupts through
    ``origin_run_id`` and carries one to four ``choice_details``, whose
    IDs ARE the non-Other ``choices`` in the same order, so a reply's
    ``choice_id`` always resolves to exactly one admitted hit. Labels and
    summaries are bounded and checked here rather than at the presentation
    layer, because an oversized candidate must be refused before it is
    published rather than truncated away afterwards.
    """

    question_id: str
    task_id: str
    task_version: int
    prompt: str
    choices: tuple[str, ...]
    input_version: int
    purpose: Literal["task_clarification", "document_choice"] = (
        PURPOSE_TASK_CLARIFICATION
    )
    origin_run_id: str | None = None
    choice_details: tuple[ChoiceDetail, ...] = ()

    def __post_init__(self) -> None:
        if not self.prompt.strip():
            raise TaskRuntimeError(
                TASK_INPUT_INVALID, "a clarification must preserve its prompt"
            )
        if not self.choices:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID, "a clarification must offer its choices"
            )
        if self.purpose not in DOCUMENT_CHOICE_PURPOSES:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                f"unknown pending question purpose: {self.purpose}",
                question_id=self.question_id,
            )
        if self.purpose == PURPOSE_DOCUMENT_CHOICE:
            _check_document_choice(self)
        else:
            _check_clarification_choices(self)

    @property
    def offered_choices(self) -> tuple[str, ...]:
        """The listed choices plus exactly one trailing Other.

        A free-text-only clarification stores the reserved token as its
        only listed choice, so it is already the complete offered list
        and must not grow a second Other.
        """
        if self.choices[-1] == OTHER_CHOICE_ID:
            return self.choices
        return (*self.choices, OTHER_CHOICE_ID)

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "prompt": self.prompt,
            "choices": list(self.offered_choices),
            "input_version": self.input_version,
            "purpose": self.purpose,
            "origin_run_id": self.origin_run_id,
            "choice_details": [row.to_state_dict() for row in self.choice_details],
        }


def _refuse(question_id: str, message: str, **detail: Any) -> TaskRuntimeError:
    return TaskRuntimeError(
        TASK_INPUT_INVALID, message, question_id=question_id, **detail
    )


def _has_control_char(value: str, *, allow_newlines: bool) -> bool:
    """True when the text carries a control character.

    Judged by the Unicode category's leading ``C`` rather than by
    ``isprintable``: the latter also reports a no-break space or an
    ideographic space as unprintable, and those are ordinary text a label
    may legitimately contain. A newline in prose is content, so
    ``allow_newlines`` exempts exactly that one character; a tab, a NUL or
    an escape travels through the wire, the log and the UI unchanged and is
    refused where the author can still see why.
    """
    return any(
        unicodedata.category(character).startswith("C")
        and not (allow_newlines and character == "\n")
        for character in value
    )


def _bounded_text(
    value: str,
    limit: int,
    field: str,
    question_id: str,
    *,
    required: bool,
    allow_newlines: bool,
) -> None:
    """Refuse one field against its own bound, named in the detail.

    Each field is checked on its own so a caller correcting a proposal is
    told which one is wrong instead of "the question is too big". The
    ceiling is inclusive and measured in UTF-8 bytes, because that is what
    the wire carries; nothing here shortens the text.
    """
    if required and not value.strip():
        raise _refuse(question_id, f"{field} must not be empty", field=field)
    if _has_control_char(value, allow_newlines=allow_newlines):
        raise _refuse(
            question_id,
            f"{field} must not carry control characters",
            field=field,
        )
    if len(value.encode("utf-8")) > limit:
        raise _refuse(
            question_id,
            f"{field} exceeds {limit} UTF-8 bytes",
            field=field,
            limit=limit,
        )


def _check_source_ref(ref: ChoiceSourceRef, question_id: str) -> None:
    if ref.source_kind not in SOURCE_KINDS:
        raise _refuse(
            question_id,
            f"unknown choice source kind: {ref.source_kind}",
            source_kind=str(ref.source_kind),
        )
    _bounded_text(
        ref.source_id,
        DOCUMENT_CHOICE_ID_BYTES,
        "source_id",
        question_id,
        required=True,
        allow_newlines=False,
    )
    _bounded_text(
        ref.version,
        DOCUMENT_CHOICE_ID_BYTES,
        "version",
        question_id,
        required=True,
        allow_newlines=False,
    )
    if ref.section is not None:
        _bounded_text(
            ref.section,
            DOCUMENT_CHOICE_LABEL_BYTES,
            "section",
            question_id,
            required=False,
            allow_newlines=False,
        )


def _check_choice_detail(detail: ChoiceDetail, question_id: str) -> None:
    _bounded_text(
        detail.choice_id,
        DOCUMENT_CHOICE_ID_BYTES,
        "choice_id",
        question_id,
        required=True,
        allow_newlines=False,
    )
    _bounded_text(
        detail.label,
        DOCUMENT_CHOICE_LABEL_BYTES,
        "label",
        question_id,
        required=True,
        allow_newlines=False,
    )
    _bounded_text(
        detail.summary,
        DOCUMENT_CHOICE_SUMMARY_BYTES,
        "summary",
        question_id,
        required=True,
        allow_newlines=True,
    )
    _check_source_ref(detail.source_ref, question_id)


def _check_clarification_choices(question: PendingQuestion) -> None:
    """Every bound a ``task_clarification`` option set must satisfy.

    The options of a clarification are the labels a person picks, so they
    carry the SAME presentation bounds as a document-choice label: at
    most :data:`MAX_DOCUMENT_CHOICES` listed options, each a non-blank
    string without control characters under
    :data:`DOCUMENT_CHOICE_LABEL_BYTES`, and no two options that read as
    the same choice. A duplicate or an unreadable label would publish a
    card where two buttons cannot be told apart, and an oversized one
    would be truncated by whatever renders it.

    The trailing-Other rule is the free-text-only shape's whole
    contract: Other is stored once, last, and never mixed among real
    options. A question with no real option stores Other as its only
    listed choice.

    The prompt ceiling is deliberately NOT applied here: the frozen wire
    contract keeps document-choice limits from retroactively truncating
    existing task questions, and old checkpoints may hold a longer
    prompt. New clarify questions build their prompt from these bounded
    labels.
    """
    positions = [
        index
        for index, choice in enumerate(question.choices)
        if choice == OTHER_CHOICE_ID
    ]
    if positions and positions != [len(question.choices) - 1]:
        raise _refuse(
            question.question_id,
            "a clarification lists Other exactly once, last",
            choice_count=len(question.choices),
        )
    listed = [
        choice for choice in question.choices if choice != OTHER_CHOICE_ID
    ]
    if len(listed) > MAX_DOCUMENT_CHOICES:
        raise _refuse(
            question.question_id,
            f"a clarification offers at most {MAX_DOCUMENT_CHOICES} "
            "listed choices",
            choice_count=len(listed),
        )
    seen: set[str] = set()
    for choice in listed:
        _bounded_text(
            choice,
            DOCUMENT_CHOICE_LABEL_BYTES,
            "choice",
            question.question_id,
            required=True,
            allow_newlines=False,
        )
        folded = choice.casefold()
        if folded in seen:
            raise _refuse(
                question.question_id,
                f"duplicate clarification choice: {choice}",
                choice=choice,
            )
        seen.add(folded)


def _check_document_choice(question: PendingQuestion) -> None:
    """Every rule a ``document_choice`` must satisfy before it is published.

    Refusal rather than repair throughout: dropping an over-limit
    candidate or renaming a duplicate would publish a question the user
    cannot answer as intended, and truncating a label would drop the
    meaning that made it worth choosing.
    """
    question_id = question.question_id
    if not question.origin_run_id or not question.origin_run_id.strip():
        raise _refuse(
            question_id,
            "a document choice must name the run it interrupts",
            origin_run_id=question.origin_run_id,
        )
    if not 1 <= len(question.choice_details) <= MAX_DOCUMENT_CHOICES:
        raise _refuse(
            question_id,
            "a document choice offers one to "
            f"{MAX_DOCUMENT_CHOICES} admitted choices",
            choice_count=len(question.choice_details),
        )
    if len(question.choices) != len(question.choice_details):
        raise _refuse(
            question_id,
            "a document choice lists exactly its choice details",
            choice_count=len(question.choices),
            detail_count=len(question.choice_details),
        )
    seen: set[str] = set()
    for position, detail in enumerate(question.choice_details):
        if detail.choice_id == OTHER_CHOICE_ID:
            raise _refuse(
                question_id,
                "a document choice detail cannot be the Other token",
                choice_id=detail.choice_id,
            )
        if detail.choice_id in seen:
            raise _refuse(
                question_id,
                f"duplicate document choice id: {detail.choice_id}",
                choice_id=detail.choice_id,
            )
        seen.add(detail.choice_id)
        if detail.choice_id != question.choices[position]:
            raise _refuse(
                question_id,
                "choice details must match the listed choices in order",
                choice_id=detail.choice_id,
            )
        _check_choice_detail(detail, question_id)
    _bounded_text(
        question.prompt,
        DOCUMENT_CHOICE_PROMPT_BYTES,
        "prompt",
        question_id,
        required=True,
        allow_newlines=True,
    )


def offer_document_choice(
    *,
    question_id: str,
    task_id: str,
    task_version: int,
    prompt: str,
    choice_details: tuple[ChoiceDetail, ...],
    origin_run_id: str,
    input_version: int,
) -> PendingQuestion:
    """Publish the admitted candidates of an ambiguous ask as a question.

    The candidate list becomes the ``choices`` in the order the details name
    it, so ``choice_details`` and the reply-resolvable ids cannot disagree;
    the trailing Other stays the projection's job and is never stored as a
    candidate. Every refusal below is checked before the record exists, so
    a proposal over the bound cannot reach a surface with a candidate
    silently dropped.
    """
    return PendingQuestion(
        question_id=question_id,
        task_id=task_id,
        task_version=task_version,
        prompt=prompt,
        choices=tuple(detail.choice_id for detail in choice_details),
        input_version=input_version,
        purpose=PURPOSE_DOCUMENT_CHOICE,
        origin_run_id=origin_run_id,
        choice_details=tuple(choice_details),
    )


@dataclass(frozen=True)
class QuestionReply:
    """A submitted reply, preserved as submitted.

    ``choice_id`` and ``free_text`` both survive. A reply naming Other
    keeps its free text beside the reserved choice token, so "the user
    said something else" never degrades into "the user picked a listed
    option".
    """

    question_id: str
    choice_id: str
    free_text: str | None = None

    def __post_init__(self) -> None:
        if not self.choice_id.strip():
            raise TaskRuntimeError(
                TASK_INPUT_INVALID, "a reply must name the choice it submits"
            )

    @property
    def is_other(self) -> bool:
        return self.choice_id == OTHER_CHOICE_ID

    def answer_text(self) -> str:
        """The reply as one string, keeping free text verbatim."""
        if self.free_text and self.is_other:
            return self.free_text
        return self.choice_id

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "question_id": self.question_id,
            "choice_id": self.choice_id,
            "free_text": self.free_text,
        }


@dataclass(frozen=True)
class Reconciliation:
    """The one committed decision made about one accepted input.

    Persisted beside the task transition and the execution disposition,
    so a reader can tell WHICH input was reconciled, WHICH version it was
    reconciled against, and what it authorised.
    ``reconciled_input_version`` is the marker dependant work checks
    before starting, so a later model or tool iteration cannot reconcile
    the same input twice.
    """

    reconciled_input_version: int
    task_id: str | None
    task_version: int | None
    disposition: str
    reason: str = ""

    def __post_init__(self) -> None:
        if self.disposition not in EXECUTION_DISPOSITIONS:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                f"unknown execution disposition: {self.disposition}",
                disposition=self.disposition,
            )

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "reconciled_input_version": self.reconciled_input_version,
            "task_id": self.task_id,
            "task_version": self.task_version,
            "disposition": self.disposition,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class RestoreRecheck:
    """The four rechecks a restore must pass before its results go current.

    Each flag is a CURRENT fact, never one inherited from the archive.
    ``source_authority_current`` is deliberately not derivable from the
    archived task's saved scope: a saved source choice is user intent,
    not live authorisation, so a caller cannot pass this flag by
    pointing at the archive.
    """

    conditions_current: bool
    source_authority_current: bool
    source_identity_version_current: bool
    evidence_applicable: bool

    def failures(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.conditions_current:
            out.append(TASK_APPLICABILITY_UNRESOLVED)
        if not self.source_authority_current:
            out.append(TASK_PERMISSION_DENIED)
        if not self.source_identity_version_current:
            out.append(TASK_CAPABILITY_UNSUPPORTED)
        if not self.evidence_applicable:
            out.append(TASK_INSUFFICIENT_EVIDENCE)
        return tuple(out)

    def passes(self) -> bool:
        return not self.failures()


@dataclass(frozen=True)
class GroundingAssessment:
    """The peer module's proposal about the CURRENT task.

    Runtime does not decide what the evidence supports; it only decides
    whether an assessment may be applied against committed task and
    source-scope state. So ``supported`` is never consulted against a
    task this module did not commit, and an empty ``material_gaps`` is
    necessary but not sufficient — the host gates still have to pass.
    """

    supported: bool
    material_gaps: tuple[str, ...] = ()
    turn_outcome: str = ""
    source_scope_version: int = 0

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "material_gaps": list(self.material_gaps),
            "turn_outcome": self.turn_outcome,
            "source_scope_version": self.source_scope_version,
        }


@dataclass(frozen=True)
class HostGates:
    """The host hard gates no amount of semantic support can skip."""

    permission: bool = True
    source_identity_version: bool = True
    receipts: bool = True
    persistence: bool = True

    def failures(self) -> tuple[str, ...]:
        out: list[str] = []
        if not self.permission:
            out.append(TASK_PERMISSION_DENIED)
        if not self.source_identity_version:
            out.append(TASK_CAPABILITY_UNSUPPORTED)
        if not self.receipts:
            out.append(TASK_DEPENDENCY_FAILURE)
        if not self.persistence:
            out.append(TASK_CHECKPOINT_FAILED)
        return tuple(out)

    def passes(self) -> bool:
        return not self.failures()


@dataclass(frozen=True)
class CancellationRecord:
    """What cancellation was requested, and what run control reported.

    Both halves are kept because they are different facts. Nothing here
    asserts an external effect was undone: ``run_control_reported`` is
    recorded verbatim and ``effect_undone`` stays ``None`` unless the
    caller supplies evidence.
    """

    requested_reason: str
    run_control_reported: str
    at_input_version: int
    task_id: str | None = None
    effect_undone: bool | None = None

    def to_state_dict(self) -> dict[str, Any]:
        return {
            "requested_reason": self.requested_reason,
            "run_control_reported": self.run_control_reported,
            "at_input_version": self.at_input_version,
            "task_id": self.task_id,
            "effect_undone": self.effect_undone,
        }


@dataclass(frozen=True)
class TaskRuntimeState:
    """Everything the runtime owns for one thread.

    Held as ONE envelope on ``AgentState`` rather than as sibling
    channels: a task transition, its reconciliation marker, its pending
    question, and its execution disposition commit together, and one key
    is the only way to make "committed together" true at the state
    boundary. The envelope is thread-scoped — ``reset_input_state``
    omits it, so a run-scoped reset, a resume, and a compaction all
    preserve task history.
    """

    current_task: TaskState | None = None
    archived_tasks: tuple[ArchivedTask, ...] = ()
    input_history: tuple[AcceptedInput, ...] = ()
    input_version: int = 0
    pending_question: PendingQuestion | None = None
    reconciliation: Reconciliation | None = None
    task_outcome: str | None = None
    cancellations: tuple[CancellationRecord, ...] = ()

    def to_state_dict(self) -> dict[str, Any]:
        """The value written to ``AgentState.task_runtime``."""
        return {
            "current_task": (
                self.current_task.to_state_dict() if self.current_task else None
            ),
            "archived_tasks": [row.to_state_dict() for row in self.archived_tasks],
            "input_history": [row.to_state_dict() for row in self.input_history],
            "input_version": self.input_version,
            "pending_question": (
                self.pending_question.to_state_dict()
                if self.pending_question
                else None
            ),
            "reconciliation": (
                self.reconciliation.to_state_dict()
                if self.reconciliation
                else None
            ),
            "task_outcome": self.task_outcome,
            "cancellations": [row.to_state_dict() for row in self.cancellations],
        }

    @classmethod
    def from_state_dict(
        cls, raw: Mapping[str, Any] | None
    ) -> TaskRuntimeState:
        """Read the stored envelope back.

        Tolerates a checkpoint written before this envelope existed: a
        legacy thread simply has no ``task_runtime`` key, and every field
        then reads as its empty default instead of raising — the
        migration question the inventory named, answered as "no
        backfill, read as empty".
        """
        data = dict(raw or {})
        current = data.get("current_task")
        question = data.get("pending_question")
        reconciliation = data.get("reconciliation")
        return cls(
            current_task=(
                _task_from_dict(current) if isinstance(current, Mapping) else None
            ),
            archived_tasks=tuple(
                _archive_from_dict(row)
                for row in _rows(data.get("archived_tasks"))
            ),
            input_history=tuple(
                _accepted_from_dict(row) for row in _rows(data.get("input_history"))
            ),
            input_version=_as_int(data.get("input_version")),
            pending_question=(
                _question_from_dict(question)
                if isinstance(question, Mapping)
                else None
            ),
            reconciliation=(
                _reconciliation_from_dict(reconciliation)
                if isinstance(reconciliation, Mapping)
                else None
            ),
            task_outcome=_as_optional_str(data.get("task_outcome")),
            cancellations=tuple(
                _cancellation_from_dict(row)
                for row in _rows(data.get("cancellations"))
            ),
        )



def _rows(value: Any) -> list[Mapping[str, Any]]:
    return [row for row in (value or []) if isinstance(row, Mapping)]


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _as_optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _task_from_dict(raw: Mapping[str, Any]) -> TaskState:
    outcome = str(raw.get("outcome") or TASK_ACTIVE)
    return TaskState(
        task_id=str(raw.get("task_id") or ""),
        goal=str(raw.get("goal") or ""),
        expected_result=str(raw.get("expected_result") or ""),
        task_version=_as_int(raw.get("task_version")) or 1,
        necessary_conditions=tuple(
            str(item) for item in raw.get("necessary_conditions") or []
        ),
        outcome=outcome if outcome in TASK_OUTCOMES else TASK_ACTIVE,
        outcome_reason=str(raw.get("outcome_reason") or ""),
        created_at_input_version=_as_int(raw.get("created_at_input_version")) or 1,
    )


def _archive_from_dict(raw: Mapping[str, Any]) -> ArchivedTask:
    return ArchivedTask(
        archive_id=str(raw.get("archive_id") or ""),
        task=_task_from_dict(raw.get("task") or {}),
        archived_at_input_version=_as_int(raw.get("archived_at_input_version")),
        run_id=_as_optional_str(raw.get("run_id")),
        evidence_refs=tuple(str(ref) for ref in raw.get("evidence_refs") or []),
    )


def _accepted_from_dict(raw: Mapping[str, Any]) -> AcceptedInput:
    kind = str(raw.get("kind") or INPUT_MESSAGE)
    payload = raw.get("payload")
    return AcceptedInput(
        input_version=_as_int(raw.get("input_version")),
        kind=kind if kind in INPUT_KINDS else INPUT_MESSAGE,
        payload=payload if isinstance(payload, str) else None,
        provenance=str(raw.get("provenance") or ""),
        question_id=_as_optional_str(raw.get("question_id")),
        archive_id=_as_optional_str(raw.get("archive_id")),
        choice_id=_as_optional_str(raw.get("choice_id")),
        run_id=_as_optional_str(raw.get("run_id")),
    )


def _choice_detail_from_dict(raw: Mapping[str, Any]) -> ChoiceDetail:
    ref = raw.get("source_ref")
    ref_row = dict(ref) if isinstance(ref, Mapping) else {}
    section = ref_row.get("section")
    return ChoiceDetail(
        choice_id=str(raw.get("choice_id") or ""),
        label=str(raw.get("label") or ""),
        summary=str(raw.get("summary") or ""),
        source_ref=ChoiceSourceRef(
            source_kind=str(ref_row.get("source_kind") or ""),  # type: ignore[arg-type]
            source_id=str(ref_row.get("source_id") or ""),
            version=str(ref_row.get("version") or ""),
            section=section if isinstance(section, str) and section else None,
        ),
    )


def _question_from_dict(raw: Mapping[str, Any]) -> PendingQuestion:
    choices = tuple(str(choice) for choice in raw.get("choices") or [])
    # The stored list ends with the Other token; ``PendingQuestion``
    # holds only the LISTED choices, so the trailing token is split back
    # off here rather than being counted as a listed choice. A
    # free-text-only clarification stores Other as its ONLY choice, so
    # stripping it would leave a question the value type refuses.
    if len(choices) > 1 and choices[-1] == OTHER_CHOICE_ID:
        choices = choices[:-1]
    purpose = str(raw.get("purpose") or PURPOSE_TASK_CLARIFICATION)
    origin_run_id = _as_optional_str(raw.get("origin_run_id"))
    return PendingQuestion(
        question_id=str(raw.get("question_id") or ""),
        task_id=str(raw.get("task_id") or ""),
        task_version=_as_int(raw.get("task_version")) or 1,
        prompt=str(raw.get("prompt") or ""),
        choices=choices,
        input_version=_as_int(raw.get("input_version")),
        # A checkpoint written before the extension has none of the three
        # keys and reads as a ``task_clarification``, which is what it was;
        # an unknown purpose degrades the same way rather than raising on a
        # read, so one unrecognisable token cannot strand a whole thread.
        purpose=(
            purpose if purpose in DOCUMENT_CHOICE_PURPOSES else PURPOSE_TASK_CLARIFICATION
        ),  # type: ignore[arg-type]
        origin_run_id=origin_run_id,
        choice_details=tuple(
            _choice_detail_from_dict(row) for row in _rows(raw.get("choice_details"))
        ),
    )


def _reconciliation_from_dict(raw: Mapping[str, Any]) -> Reconciliation:
    disposition = str(raw.get("disposition") or DISPOSITION_NONE)
    version = raw.get("task_version")
    return Reconciliation(
        reconciled_input_version=_as_int(raw.get("reconciled_input_version")),
        task_id=_as_optional_str(raw.get("task_id")),
        task_version=_as_int(version) if version is not None else None,
        disposition=(
            disposition
            if disposition in EXECUTION_DISPOSITIONS
            else DISPOSITION_NONE
        ),
        reason=str(raw.get("reason") or ""),
    )


def _cancellation_from_dict(raw: Mapping[str, Any]) -> CancellationRecord:
    undone = raw.get("effect_undone")
    return CancellationRecord(
        requested_reason=str(raw.get("requested_reason") or ""),
        run_control_reported=str(raw.get("run_control_reported") or ""),
        at_input_version=_as_int(raw.get("at_input_version")),
        task_id=_as_optional_str(raw.get("task_id")),
        effect_undone=undone if isinstance(undone, bool) else None,
    )



# ----- the model's proposal about one accepted input -----


@dataclass(frozen=True)
class ReconciliationProposal:
    """What the model proposed about one accepted input.

    This is "the model proposes, the host validates" on the wire. It
    carries no authority: :func:`decide` checks it against the latest
    committed state and commits a :class:`Reconciliation` or raises.

    ``taskless`` states the taskless disposition up front — an ordinary
    greeting or off-topic turn keeps no task, and asking the model for a
    task on one is how a fabricated task would appear.
    ``switch`` means the incoming input replaces the current task: the
    outgoing task is archived and the new one committed in the SAME
    transition, so a reader never recovers one half of a switch alone.
    """

    kind: str
    taskless: bool = False
    switch: bool = False
    goal: str = ""
    expected_result: str = ""
    necessary_conditions: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    question: PendingQuestion | None = None
    archive_id: str | None = None
    recheck: RestoreRecheck | None = None
    run_id: str | None = None
    evidence_refs: tuple[str, ...] = ()
    reason: str = ""

    def __post_init__(self) -> None:
        if self.kind not in INPUT_KINDS:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                f"unknown input kind: {self.kind}",
                input_kind=self.kind,
            )
        unknown = [
            name for name in self.changed if name not in MATERIAL_CHANGE_KINDS
        ]
        if unknown:
            raise TaskRuntimeError(
                TASK_INPUT_INVALID,
                f"changed fields are not material change kinds: {unknown}",
                changed=list(unknown),
            )


def default_task_id(accepted: AcceptedInput) -> str:
    """The fallback task id for an accepted input with no factory wired.

    Derived from the input version, so a thread read back after a restart
    names the same task it did before.
    """
    return f"task-{accepted.input_version}"
