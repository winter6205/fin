"""The PUBLIC document-choice admission surface for the workspace.

The workspace send door has to answer two questions about a document
reply before it accepts the input, and both questions are the GRAPH's
questions, not its own:

* has this exact ``(question_id, choice_id)`` pick already been accepted?
  (the shared contract's "an identical retry is recognized in accepted
  history")
* does this pick resolve to the thread's committed pending question, and
  is that question the run the reply lands on?

The graph already owns both answers — in the QUESTION capability's
``document_choice_interrupt_node`` body, where they are the only decision
the node makes — but it kept them as module-private helpers. Reaching in
from another module across a ``_``-prefixed name makes an internal
refactor a cross-module break: the signature a private function happens
to have this week becomes part of another module's contract.

So this module is the seam. It re-derives the SAME answers from the
committed envelope and the thread's own pending question, which are both
already public, and exposes them under public names. Two properties keep
that from becoming a second decision authority:

* the identity is one function with one caller (``accepted_document_reply``),
  so the composite key is still written once and matched once;
* the membership check reads the question's OWN ``offered_choices`` and
  its OWN ``origin_run_id`` — the same two fields the graph's validator
  reads — so a question admitted here is admitted there.

Neither function admits anything. They answer "is this reply already
recorded" and "does this pick resolve"; the graph's node remains the only
place a reply is reconciled, and the workspace uses these to REFUSE EARLY,
before ``accept_input`` has advanced ``input_version``.
"""

from __future__ import annotations

from fin.host.task.task_runtime_types import (
    INPUT_REPLY,
    OTHER_CHOICE_ID,
    PURPOSE_DOCUMENT_CHOICE,
    AcceptedInput,
    PendingQuestion,
    TaskRuntimeState,
)


def document_choice_idempotency_key(
    runtime: TaskRuntimeState, *, question_id: str, choice_id: str
) -> str:
    """The identity one accepted document reply is recognized by.

    The shared contract names the tuple ``(thread, question_id, choice_id,
    free_text)``. The thread is implicit in the envelope this history
    belongs to, and a choice click carries no free text at all, so the
    free-text slot contributes nothing here.

    ``runtime`` is accepted so every caller passes the same envelope the
    key is checked against; the identity itself names only the question
    and the choice.
    """
    return f"{question_id}/{choice_id}"


def document_choice_resume_value(
    choice_id: str, free_text: str | None = None
) -> dict[str, str | None]:
    """The ``Command(resume=...)`` value one document reply carries.

    The resume value is the ONLY channel the paused run has for the
    operator's submission: it cannot read the HTTP body, and the graph's
    node has to accept the reply from it alone. Carrying the bare
    ``choice_id`` therefore loses the one thing the reserved ``other``
    token exists to carry — the user's rephrase
    (``specs/question-task-runtime-contract.md:90``: "Other submits
    ``choice_id=other`` and preserves the user's exact nonempty
    ``free_text`` as the reply payload").

    A dict, not a tuple or a concatenated string: the field names are
    the wire's own, and a value the graph cannot read is one it reopens
    the question on rather than one it mis-accepts.

    ``free_text`` rides ONLY the reserved Other token. A candidate click
    carries no user-authored text, and reading one off a click would
    fabricate prose from the message composer — the very thing the same
    contract line forbids ("A click has ``payload=null`` ... do not
    fabricate user-authored prose from the label"). Deciding that here,
    at the one constructor, is what keeps the two legs from being decided
    differently by the two doors.

    This is the ONE constructor, so the send door and the ``/resume``
    route cannot build different resume values for one reply.
    """
    if choice_id.strip() != OTHER_CHOICE_ID:
        return {"choice_id": choice_id, "free_text": None}
    text = free_text if free_text is not None and free_text.strip() else None
    return {"choice_id": choice_id, "free_text": text}


def accepted_document_reply(
    runtime: TaskRuntimeState, *, question_id: str, choice_id: str
) -> AcceptedInput | None:
    """The FIRST accepted record for this exact document pick, if any.

    Returning the stored record — rather than refusing — is what keeps a
    double click from advancing ``input_version`` twice: the record names
    the input version the FIRST pick already reconciled.

    The COMPOSITE key is what is matched, never the bare candidate id: an
    ordinary ``task_clarification`` reply stores the bare id on the accept
    path, and short-circuiting that one would change a door this pre-check
    was never asked about.
    """
    key = document_choice_idempotency_key(
        runtime, question_id=question_id, choice_id=choice_id
    )
    for row in runtime.input_history:
        if row.kind == INPUT_REPLY and row.choice_id == key:
            return row
    return None


def resolved_document_choice_pick(
    runtime: TaskRuntimeState,
    *,
    origin_run_id: str | None,
    raw_pick: str,
) -> PendingQuestion | None:
    """The committed question this pick resolves, or ``None``.

    Same order as the graph's own validate-then-reopen loop, and the same
    fields: there must be a pending question, it must be a
    ``document_choice``, the paused run must be THIS run
    (``origin_run_id`` equality), and the pick must be one of the
    question's own listed ids. The trailing ``other`` is a member like
    any other; opening it is presentation-only.

    ``origin_run_id`` is a plain value rather than a forged ``RunnableConfig``
    for one reason: the caller reads it off the checkpoint through
    ``graph.checkpoint_run_id`` and has no runnable to hand over. A hand-
    forged config dict that happens to be shaped like the one the graph
    builds is a second place where the binding is decided, and it can
    drift from the real one without any test noticing.
    """
    question = runtime.pending_question
    if question is None or question.purpose != PURPOSE_DOCUMENT_CHOICE:
        return None
    if origin_run_id is None or question.origin_run_id != origin_run_id:
        return None
    picked = raw_pick.strip()
    if picked not in question.offered_choices:
        return None
    return question


__all__ = [
    "accepted_document_reply",
    "document_choice_idempotency_key",
    "document_choice_resume_value",
    "resolved_document_choice_pick",
]
