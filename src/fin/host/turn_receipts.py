"""One receipt per logical turn, projected from what the run observed.

The store owns the receipt's contract — its key, its compare-and-swap
decisions, and the single transaction that commits a terminal receipt
next to its transcript row. This module owns only the projection: it
reads the stored row, folds one lifecycle observation into a draft, and
hands that draft back to the same shared decision functions both stores
already call. Nothing here re-implements compare-and-swap, nothing here
writes a row, and nothing here invents a fact the run did not observe.

WHY the turn identity is never re-minted here: a receipt is addressed by
(thread, turn) and this module creates one from exactly one call site —
the send door's own acceptance. A continuation, a resume, a background
auto-resume and a recovery therefore have no way to bring a receipt into
existence at all, so the only way one logical turn could hold two is if
a caller handed two different turn ids for it, which is the identity the
store's primary key exists to refuse.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
from typing import Final, Sequence

from opentelemetry.sdk.trace import ReadableSpan

from fin.host.grounded.route_intent import normalize_route_intent, normalize_route_source
from fin.host.run_budgets import STAGE_CLEANUP, STAGE_EXECUTE, StageOutcome
from fin.host.thread_store import RunRecord, ThreadStore
from fin.host.turn_receipt_store import (
    FAILURE_KINDS,
    STAGE_METRIC_KEYS,
    CancellationReason,
    ReceiptNotFoundError,
    ReceiptRevisionConflict,
    ReceiptStore,
    RouteIntent,
    RouteSource,
    SemanticResult,
    TechnicalResult,
    TurnReceipt,
    default_receipt_store,
)
from fin.host.task.task_runtime_types import (
    INPUT_MESSAGE,
    INPUT_RESTORE,
    AcceptedInput,
)

logger = logging.getLogger(__name__)


#: The stream classifications that name a receipt fact of their own.
#:
#: The other stream classes describe transports the receipt vocabulary
#: deliberately does not name, so they keep their exact class on the
#: transcript row and leave the receipt's cause null. Collapsing them
#: into ``internal`` would erase the distinction the operator's own label
#: already carries; the one class that IS a receipt fact — a model
#: invocation passing its deadline — keeps its own name.
_STREAM_FAILURE_TOKENS: Final[dict[str, str]] = {
    "timeout": "model_timeout",
}

#: A raise with no classification of its own takes the generic bucket. A
#: typed Task Runtime cause and a named Corpus failure never land here:
#: both are already allowlisted and pass through unchanged.
_UNCLASSIFIED_FAILURE_TOKEN: Final[str] = "internal"

#: The ends that are a cancellation of the run rather than its outcome.
#:
#: A stop is the operator's own action; a timeout is an executed phase
#: passing its deadline. A mid-flight raise cancels nothing — the run
#: failed on its own terms — so it carries no cancellation reason.
_CANCELLATION_BY_TECHNICAL_RESULT: Final[dict[str, CancellationReason]] = {
    "stopped": "user_stop",
    "timeout": "phase_deadline",
}


def failure_token(refuse_kind: str | None) -> str | None:
    """The receipt's ``failure_kind`` for one stream classification.

    ``None`` when the classification names no receipt token. A refusal
    the run raised on its own terms — a closed bound, a bounded call, a
    deliberate refusal — is not an operational failure, so it does not
    acquire a cause here either.
    """
    if not refuse_kind:
        return None
    if refuse_kind in FAILURE_KINDS:
        # A Task Runtime cause keeps its exact ``task_*`` token and a
        # named Corpus failure its exact wire token; the receipt records
        # both rather than a paraphrase of either.
        return refuse_kind
    if refuse_kind in _STREAM_FAILURE_TOKENS:
        return _STREAM_FAILURE_TOKENS[refuse_kind]
    if refuse_kind == "unknown":
        return _UNCLASSIFIED_FAILURE_TOKEN
    return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _route_projection(
    track_decision: object,
) -> tuple[RouteSource | None, RouteIntent | None]:
    """Project only route facts that survive the shared wire vocabulary."""
    if not isinstance(track_decision, Mapping):
        return None, None
    intent = normalize_route_intent(track_decision.get("route_intent"))
    if intent is None:
        return None, None
    return normalize_route_source(track_decision.get("route_source")), intent


def _semantic_result_for_run(
    semantic_result: SemanticResult | None,
    *,
    route_intent: RouteIntent | None,
    semantic_outcome: str | None,
) -> SemanticResult | None:
    """Refine receipt semantics from explicit runtime observations only."""
    if route_intent == "policy_refuse":
        return "policy_refuse"
    if semantic_outcome == "insufficient_evidence":
        return "evidence_refuse"
    return semantic_result


def _merge_stage(
    prior: dict[str, int | bool],
    observed: dict[str, int | bool],
) -> dict[str, int | bool]:
    """One stage key across every stretch this turn has run.

    A continuation adds its own stretch to the prefix's: they are
    separate invocations of the same stage, so their calls and elapsed
    time add, while completeness holds only when every stretch's end was
    observed.
    """
    return {
        "calls": int(prior["calls"]) + int(observed["calls"]),
        "elapsed_ms": int(prior["elapsed_ms"]) + int(observed["elapsed_ms"]),
        "complete": bool(prior["complete"]) and bool(observed["complete"]),
    }


def _stretch_work(outcomes: Sequence[StageOutcome]) -> tuple[int, bool]:
    """One stretch's observed work, and whether its end was observed."""
    elapsed = 0
    complete = False
    for outcome in outcomes:
        if outcome.stage == STAGE_EXECUTE:
            elapsed = outcome.elapsed_ms
            # An interrupted stretch never reached its end, so its
            # elapsed time is the lower bound that was seen, not a total.
            complete = outcome.complete
    return elapsed, complete


#: The four keys that break the model's inclusive total down, and the
#: executor's own names for them. Every invocation lands in exactly one
#: of them, so ``model`` is their sum and no reader has to subtract a
#: breakdown out of the whole it is a part of.
MODEL_BREAKDOWN_KEYS: Final[tuple[str, ...]] = (
    "runtime_reconciliation",
    "grounded_decision",
    "tool_planning",
    "other_model",
)

#: The receipt's inclusive model key, spelled here rather than imported:
#: the executor's two stage names are deliberately outside this
#: vocabulary, so a model call the executor never timed has no stage name
#: of its own to reuse.
_STAGE_MODEL: Final[str] = "model"


def projected_stage_metrics(
    outcomes: Sequence[StageOutcome],
    prior: dict[str, dict[str, int | bool]] | None = None,
    model_calls: dict[str, int] | None = None,
) -> dict[str, dict[str, int | bool]]:
    """The turn's twelve stages from the executor's own clock.

    The release stage is one of the twelve and is projected as measured:
    a release that was waited for counts one started call whether or not
    it finished inside the grace, and one that was never reached is
    reported incomplete rather than as a known absence.

    The model's own keys come from the run's model-call boundary rather
    than from its clock. Each invocation the graph reported is counted
    once, in the one breakdown it belongs to, and ``model`` is the sum of
    those — the inclusive total is the whole set, not a second
    measurement of it. The breakdown keys stay ``complete: false``
    because no clock runs around a model call here: the counts are
    durably observed, the elapsed time is not observed at all, and a
    stage that reports a zero it never measured must not read as one.

    ``model_calls`` says which of the two "no calls" cases this is. An
    empty mapping is the boundary being WATCHED and seeing nothing: a
    measured zero. ``None`` is no observation at all, and then the keys
    keep the "nothing observed" shape a stage this executor does not
    clock carries, so the report never becomes a claim that no call was
    made.

    The streamed stretch is deliberately NOT spread over the model keys.
    It is the whole run's observed work — model calls, retrieval and
    blocking I/O together — so folding it into ``model`` would make the
    inclusive model total a different quantity from every breakdown
    beneath it. It lands on the receipt's work fields instead.

    A stage this executor does not clock is reported incomplete at a zero
    lower bound: it is not known to have been unstarted, and a zero
    marked complete would claim an absence nobody observed.
    """
    projected: dict[str, dict[str, int | bool]] = {
        key: {"calls": 0, "elapsed_ms": 0, "complete": False}
        for key in STAGE_METRIC_KEYS
    }
    for outcome in outcomes:
        if outcome.stage != STAGE_CLEANUP:
            continue
        # A release the ledger timed is a release that began: the grace
        # bounds how long its end is WAITED for, never whether it
        # started. An untouched ledger reports zero elapsed and an
        # unfinished stage, which is the one combination that means it
        # was never reached.
        started = outcome.complete or outcome.elapsed_ms > 0
        observed = {
            "calls": 1 if started else 0,
            "elapsed_ms": outcome.elapsed_ms,
            "complete": outcome.complete,
        }
        base = prior[STAGE_CLEANUP] if prior is not None else None
        projected[STAGE_CLEANUP] = (
            observed if base is None else _merge_stage(base, observed)
        )
    for key in MODEL_BREAKDOWN_KEYS:
        projected[key] = _merge_calls(prior, key, model_calls)
    projected[_STAGE_MODEL] = _merge_calls(
        prior, _STAGE_MODEL, model_calls, total=True
    )
    return projected


def _merge_calls(
    prior: dict[str, dict[str, int | bool]] | None,
    key: str,
    model_calls: dict[str, int] | None,
    *,
    total: bool = False,
) -> dict[str, int | bool]:
    """One model key across every stretch this turn has run.

    Separate stretches make separate invocations, so their counts add.
    ``total`` is the inclusive ``model`` key: it is the sum of the four
    breakdowns, which is the same set of invocations the breakdowns each
    count a disjoint slice of, so the sum can never exceed the whole.
    """
    observed = 0
    if model_calls is not None:
        if total:
            observed = sum(model_calls.get(k, 0) for k in MODEL_BREAKDOWN_KEYS)
        else:
            observed = model_calls.get(key, 0)
    base = prior[key] if prior is not None else None
    entry = {"calls": observed, "elapsed_ms": 0, "complete": False}
    return entry if base is None else _merge_stage(base, entry)


def _clocked_stages_finished(
    outcomes: Sequence[StageOutcome], metrics: dict[str, dict[str, int | bool]]
) -> bool:
    """Whether every stage this stretch actually timed also finished.

    ``accounting_complete`` and a stage's own ``complete`` answer the same
    question from two sides — are these measurements totals, or the lower
    bounds an interruption left — so a stage the executor timed and did
    not close cannot sit beneath a flag claiming nothing was left
    unfinished. A release that overran its grace is exactly that.

    A stage the executor does not time is a different thing: it holds no
    observation that could be incomplete, so it takes no part in the
    verdict. Counting it here would leave every turn, however cleanly it
    ran, reporting its accounting forever open.
    """
    return all(
        bool(metrics[outcome.stage]["complete"])
        for outcome in outcomes
        if outcome.stage in metrics
    )


def _accounted_for(
    prior: TurnReceipt,
    accounted: bool,
    outcomes: Sequence[StageOutcome],
    metrics: dict[str, dict[str, int | bool]],
) -> bool:
    """Whether the turn's accumulated work total is a total.

    A running receipt has never been measured — the receipt begins with
    no accounting and only a pause or an end writes any — so the first
    stretch that writes one decides on its own. After that, a prefix
    already on the row holds the answer.
    """
    if prior.execution_state == "running":
        closed = accounted
    else:
        closed = bool(prior.accounting_complete and accounted)
    return closed and _clocked_stages_finished(outcomes, metrics)


class TurnReceiptProjector:
    """Folds one run lifecycle onto the one receipt of its logical turn.

    Every method reads the stored row first and writes through the
    store's own compare-and-swap, so a second observer of the same turn
    overwrites nothing. A turn with no receipt is reported as ``None``
    rather than started here: these paths persist transcript rows for
    turns that predate the receipt, and dropping a real turn row over a
    missing projection would trade a record for a summary of it.
    """

    def __init__(self, store: ReceiptStore) -> None:
        self._store = store
        self._span_sink_lock = threading.Lock()
        self._finished_span_sink: (
            Callable[[Sequence[ReadableSpan]], object] | None
        ) = None

    @property
    def store(self) -> ReceiptStore:
        return self._store

    @property
    def finished_span_sink(
        self,
    ) -> Callable[[Sequence[ReadableSpan]], object] | None:
        with self._span_sink_lock:
            return self._finished_span_sink

    def bind_finished_span_sink(
        self, sink: Callable[[Sequence[ReadableSpan]], object]
    ) -> Callable[[], None]:
        """Bind this application's nonblocking finished-span handoff."""
        with self._span_sink_lock:
            self._finished_span_sink = sink

        def release() -> None:
            with self._span_sink_lock:
                if self._finished_span_sink is sink:
                    self._finished_span_sink = None

        return release

    def begin(
        self,
        *,
        thread_id: str,
        turn_id: str,
        accepted_input_version: int,
        started_at: datetime | None = None,
    ) -> TurnReceipt:
        """Create this turn's receipt once, at the turn's own start.

        A repeat for the same key is the store's no-op, so the same
        identity started twice cannot append a second turn.
        """
        return self._store.begin_turn(
            thread_id=thread_id,
            turn_id=turn_id,
            started_at=started_at or _now(),
            accepted_input_version=accepted_input_version,
        )

    def read(self, thread_id: str, turn_id: str) -> TurnReceipt | None:
        return self._store.read_receipt(thread_id, turn_id)

    def delete_thread(self, thread_id: str) -> list[TurnReceipt]:
        """Retire ``thread_id`` through the store that owns the transaction.

        A delegation rather than a projection: the retirement is one
        decision about three tables, it belongs to the store, and this
        handle exists so a door can ask for it without reaching past the
        projector into ``.store``.
        """
        return self._store.delete_thread(thread_id)

    def accepted_reply(
        self, *, thread_id: str, turn_id: str, input_version: int
    ) -> TurnReceipt | None:
        """Count one newly accepted clarification on this turn.

        Deduplicated by the Task Runtime version the reply actually
        reached, not by this receipt's own projection: an unrelated
        accepted input may have advanced the thread's version in between
        and the reply still counts exactly once. A repeat of the same
        version adds no count and moves no revision.

        The turn returns to ``running`` with its semantic result cleared,
        because the continued run has not established its next result
        yet and the pause's verdict is no longer this turn's. The work
        the paused prefix already recorded is left alone: this
        transition accepted an input, it did not run a stretch.
        """
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None:
            logger.warning(
                "turn %s on thread %s accepted a reply with no receipt",
                turn_id,
                thread_id,
            )
            return None
        counted = self._store.record_accepted_input(
            thread_id=thread_id,
            turn_id=turn_id,
            expected_revision=prior.revision,
            input_version=input_version,
        )
        return self._cas(
            replace(
                counted,
                execution_state="running",
                semantic_result=None,
                # Ending the pause settles the wait it was holding, so
                # the wait is banked here rather than at the end.
                user_wait_ms=_waited_since(prior, _now()),
            ),
            counted,
        )

    def resume_started(self, *, thread_id: str, turn_id: str) -> TurnReceipt | None:
        """Resume a paused turn without inventing an accepted input."""
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None or prior.execution_state != "paused":
            return prior
        now = _now()
        return self._cas(
            replace(
                prior,
                execution_state="running",
                semantic_result=None,
                user_wait_ms=_waited_since(prior, now),
            ),
            prior,
        )

    def _reconcile_input_history_lower_bound(
        self,
        prior: TurnReceipt,
        turn_inputs: Sequence[AcceptedInput],
        thread_id: str,
        turn_id: str,
    ) -> TurnReceipt:
        """Add only newly visible facts when the turn's start is unknown."""
        newest_version = max(accepted.input_version for accepted in turn_inputs)
        later_inputs = tuple(
            accepted
            for accepted in turn_inputs
            if accepted.input_version > prior.accepted_input_version
        )
        if newest_version < prior.accepted_input_version:
            logger.warning(
                "recovery_inconsistent: checkpoint input history for turn "
                "%s on thread %s trails receipt version %d",
                turn_id,
                thread_id,
                prior.accepted_input_version,
            )
        return self._cas(
            replace(
                prior,
                accepted_input_count=prior.accepted_input_count + len(later_inputs),
                accepted_input_version=max(
                    prior.accepted_input_version, newest_version
                ),
                accounting_complete=False,
            ),
            prior,
        )

    @staticmethod
    def _has_complete_input_projection(
        prior: TurnReceipt,
        turn_inputs: Sequence[AcceptedInput],
    ) -> bool:
        first_turn_input = min(
            turn_inputs, key=lambda accepted: accepted.input_version
        )
        has_turn_start = first_turn_input.kind in {INPUT_MESSAGE, INPUT_RESTORE}
        receipt_matches_history = (
            len(turn_inputs) == prior.accepted_input_count
            and max(accepted.input_version for accepted in turn_inputs)
            == prior.accepted_input_version
        )
        return has_turn_start or receipt_matches_history

    def reconcile_accepted_inputs(
        self,
        *,
        thread_id: str,
        turn_id: str,
        input_history: Sequence[AcceptedInput],
    ) -> TurnReceipt | None:
        """Project this turn's accepted inputs from durable checkpoint history.

        The checkpoint is the task-runtime authority after restart. Its
        bounded history supplies only a lower bound when earlier entries have
        rolled off, so this projection counts only matching entries still
        visible and never infers a missing total. Replaying the same history
        is a no-op, and the receipt's original start and identity stay fixed.
        """
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None or prior.execution_state == "terminal":
            return prior
        turn_inputs = tuple(
            accepted for accepted in input_history if accepted.run_id == turn_id
        )
        if not turn_inputs:
            logger.warning(
                "recovery_inconsistent: checkpoint history has no accepted input "
                "for turn %s on thread %s",
                turn_id,
                thread_id,
            )
            return self._cas(replace(prior, accounting_complete=False), prior)

        if not self._has_complete_input_projection(prior, turn_inputs):
            # EXIT: missing start means a reply-only history cannot replace
            # the known receipt prefix, even when the history has not filled.
            logger.warning(
                "recovery_inconsistent: checkpoint history lacks the "
                "original input for turn %s on thread %s",
                turn_id,
                thread_id,
            )
            return self._reconcile_input_history_lower_bound(
                prior, turn_inputs, thread_id, turn_id
            )

        # The same-turn start is visible, so the matching rows provide the
        # complete accepted-input projection for this turn.
        draft = replace(
            prior,
            accepted_input_count=len(turn_inputs),
            accepted_input_version=max(
                accepted.input_version for accepted in turn_inputs
            ),
        )
        return self._cas(draft, prior)

    def paused(
        self,
        *,
        thread_id: str,
        turn_id: str,
        semantic_result: SemanticResult | None,
        track_decision: object = None,
        stage_metrics: Sequence[StageOutcome] = (),
        catalog_warning_count: int | None = None,
        model_calls: dict[str, int] | None = None,
    ) -> TurnReceipt | None:
        """Record the deliberate wait this turn is now in.

        The prefix's observed work and its stage measurements are written
        with the pause, so the continuation's terminal commit adds to
        them instead of reporting the remainder alone. The committed
        ``updated_at`` is the instant the wait started, which is what
        the terminal commit later measures the wait from.
        """
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None:
            return None
        if prior.execution_state == "terminal":
            # A terminal turn is finished, and a pause observed after it
            # describes a state this receipt can no longer report.
            return prior
        work, accounted = _stretch_work(stage_metrics)
        now = _now()
        metrics = projected_stage_metrics(
            stage_metrics, prior.stage_metrics, model_calls
        )
        route_source, route_intent = _route_projection(track_decision)
        draft = replace(
            prior,
            execution_state="paused",
            semantic_result=semantic_result,
            route_source=route_source,
            route_intent=route_intent,
            work_elapsed_ms=prior.work_elapsed_ms + work,
            accounting_complete=_accounted_for(prior, accounted, stage_metrics, metrics),
            user_wait_ms=_waited_since(prior, now),
            stage_metrics=metrics,
            **_warnings(catalog_warning_count),
        )
        return self._cas(draft, prior)

    def commit_terminal(
        self,
        *,
        thread_id: str,
        turn_id: str,
        technical_result: TechnicalResult,
        run: RunRecord,
        semantic_result: SemanticResult | None = None,
        failure_kind: str | None = None,
        cancellation_reason: CancellationReason | None = None,
        stage_metrics: Sequence[StageOutcome] = (),
        terminal_at: datetime | None = None,
        catalog_warning_count: int | None = None,
        model_calls: dict[str, int] | None = None,
    ) -> TurnReceipt | None:
        """Commit the terminal receipt and its transcript row together.

        Returns the committed receipt, or ``None`` when there is no
        receipt to commit onto or another finalizer won the revision —
        the caller then persists the transcript row alone rather than
        dropping it, which the keyed append makes idempotent.
        """
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None:
            logger.warning(
                "turn %s on thread %s finished with no receipt; its "
                "transcript row is persisted without one",
                turn_id,
                thread_id,
            )
            return None
        draft = self._terminal_draft(
            prior,
            technical_result=technical_result,
            semantic_result=semantic_result,
            failure_kind=failure_kind,
            cancellation_reason=cancellation_reason,
            stage_metrics=stage_metrics,
            terminal_at=terminal_at or _now(),
            catalog_warning_count=catalog_warning_count,
            model_calls=model_calls,
            track_decision=run.track_decision,
            semantic_outcome=run.semantic_outcome,
        )
        try:
            return self._store.commit_terminal(
                draft, expected_revision=prior.revision, run=run
            )
        except ReceiptNotFoundError:
            logger.warning(
                "the receipt for turn %s on thread %s vanished before its "
                "terminal commit",
                turn_id,
                thread_id,
            )
            return None
        except ReceiptRevisionConflict:
            logger.warning(
                "the receipt for turn %s on thread %s lost a revision race; "
                "the stored revision stands",
                turn_id,
                thread_id,
            )
            return None

    def stream_abandoned(self, *, thread_id: str, turn_id: str) -> TurnReceipt | None:
        """Record the turn whose stream was abandoned before it ended.

        The transport ends every run stream through one ``finally``,
        which fires equally on a finished run, on a refused one and on
        a client that walked away. What separates them is whether the
        run reported an end at all: a turn still ``running`` after its
        wire is gone reported nothing, and leaving it there would hold
        it open forever with no terminal instant and nothing to tell an
        abandoned turn from one still in flight.

        Only the receipt moves. No transcript row is written, because a
        turn that was abandoned has no answer to record — the row
        belongs to a run that reported one.

        What this does NOT do is recover the run: the checkpoint is
        left exactly where the abandoned stream left it, so an operator
        resume still finds the turn where it stopped.
        """
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None:
            return None
        if prior.execution_state != "running":
            # The turn already reported an end — finished, refused, or
            # deliberately waiting. A stream that ends after that is the
            # ordinary shape of a run, not a client that walked away.
            return None
        now = _now()
        draft = replace(
            prior,
            execution_state="terminal",
            technical_result="stopped",
            cancellation_reason="client_disconnect",
            terminal_at=now,
            user_wait_ms=_waited_since(prior, now),
            # The stretch that ran was measured; the one the abandoned
            # wire would have carried is not, so the turn's totals are a
            # lower bound from here on even where a pause had closed them.
            accounting_complete=False,
        )
        return self._cas(draft, prior)

    def stopped(
        self,
        *,
        thread_id: str,
        turn_id: str,
        cancellation_reason: CancellationReason | None,
    ) -> TurnReceipt | None:
        """Record a stopped run without adding a transcript row."""
        prior = self._store.read_receipt(thread_id, turn_id)
        if prior is None or prior.execution_state == "terminal":
            return prior
        now = _now()
        draft = replace(
            prior,
            execution_state="terminal",
            technical_result="stopped",
            semantic_result=None,
            cancellation_reason=cancellation_reason,
            terminal_at=now,
            user_wait_ms=_waited_since(prior, now),
            accounting_complete=False,
        )
        return self._cas(draft, prior)

    # ----- the drafts -----

    def _terminal_draft(
        self,
        prior: TurnReceipt,
        *,
        technical_result: TechnicalResult,
        semantic_result: SemanticResult | None,
        failure_kind: str | None,
        cancellation_reason: CancellationReason | None,
        stage_metrics: Sequence[StageOutcome],
        terminal_at: datetime,
        catalog_warning_count: int | None = None,
        model_calls: dict[str, int] | None = None,
        track_decision: object,
        semantic_outcome: str | None,
    ) -> TurnReceipt:
        """The terminal projection of one run.

        A turn that is ALREADY terminal keeps its own instants, its own
        work total and its own stage measurements. Re-adding a stretch
        to an already-counted total, or minting a fresh terminal instant,
        would make a duplicate finalizer look like a content change and
        advance the revision of a turn that did not change.
        """
        if prior.execution_state == "terminal":
            work_elapsed_ms = prior.work_elapsed_ms
            accounting_complete = prior.accounting_complete
            metrics = prior.stage_metrics
            stamped = prior.terminal_at or terminal_at
        else:
            work, accounted = _stretch_work(stage_metrics)
            work_elapsed_ms = prior.work_elapsed_ms + work
            metrics = projected_stage_metrics(
                stage_metrics, prior.stage_metrics, model_calls
            )
            accounting_complete = _accounted_for(
                prior, accounted, stage_metrics, metrics
            )
            stamped = terminal_at
        route_source, route_intent = _route_projection(track_decision)
        semantic_result = _semantic_result_for_run(
            semantic_result,
            route_intent=route_intent,
            semantic_outcome=semantic_outcome,
        )
        return replace(
            prior,
            execution_state="terminal",
            technical_result=technical_result,
            semantic_result=semantic_result,
            route_source=route_source,
            route_intent=route_intent,
            failure_kind=failure_kind,
            cancellation_reason=cancellation_reason,
            terminal_at=stamped,
            work_elapsed_ms=work_elapsed_ms,
            user_wait_ms=_waited_since(prior, stamped),
            accounting_complete=accounting_complete,
            stage_metrics=metrics,
            **_warnings(catalog_warning_count),
        )

    def _cas(self, draft: TurnReceipt, prior: TurnReceipt) -> TurnReceipt:
        try:
            return self._store.write_receipt(
                draft, expected_revision=prior.revision
            )
        except ReceiptRevisionConflict:
            # The row moved under this observer. The stored row stands:
            # it is as true as anything this process can say, and
            # overwriting it would make the receipt less truthful.
            # Said out loud for the same reason the terminal commit says
            # it: a write that silently does not land reads as a write
            # that did.
            logger.warning(
                "the receipt for turn %s on thread %s lost a revision race; "
                "the stored revision stands",
                draft.turn_id,
                draft.thread_id,
            )
            stored = self._store.read_receipt(draft.thread_id, draft.turn_id)
            return draft if stored is None else stored


def _warnings(catalog_warning_count: int | None) -> dict[str, int]:
    """The observed warning count, or the row's own when none was measured.

    A caller that has no publication store to ask — a deployment with no
    attachments at all — measures nothing, and the column it never
    observed keeps whatever the turn already established rather than
    being reset to a zero that would read as a clean measurement. A
    count of zero from a caller that DID look is a real zero and lands
    like any other measurement.
    """
    if catalog_warning_count is None:
        return {}
    return {"catalog_warning_count": catalog_warning_count}


def _waited_since(prior: TurnReceipt, now: datetime) -> int:
    """This turn's deliberate wait, plus the interval still running.

    A receipt's committed ``updated_at`` is the instant its last state
    took hold, so while the turn sits paused it IS the start of the
    wait. Every write that leaves the paused state banks the interval
    it observed, which keeps the operator's thinking time out of the
    work total no matter which write ends the pause.
    """
    if prior.execution_state != "paused" or prior.updated_at > now:
        return prior.user_wait_ms
    return prior.user_wait_ms + int(
        (now - prior.updated_at).total_seconds() * 1000
    )


def cancellation_for(technical_result: str) -> CancellationReason | None:
    """The cancellation one technical end implies, if it implies one."""
    return _CANCELLATION_BY_TECHNICAL_RESULT.get(
        technical_result  # type: ignore[arg-type]
    )


#: A durable history is named by its DSN, so the cache is a plain dict
#: over that name — two instances on one database are one history and
#: must share a projector, or a background resume would not see the
#: receipts its own door wrote.
_DURABLE_PROJECTORS: "dict[str, TurnReceiptProjector]" = {}

#: An in-process history has no name, so the store instance is it — held
#: WEAKLY, or this module pins every store it has ever been handed for
#: the life of the process. Weakness also keeps the key honest: an
#: ``id()`` would be reused by the next store to land at that address.
_LOCAL_PROJECTORS: "weakref.WeakKeyDictionary[ThreadStore, TurnReceiptProjector]" = (
    weakref.WeakKeyDictionary()
)
_PROJECTOR_LOCK = threading.Lock()


def default_receipts(threads: ThreadStore) -> TurnReceiptProjector:
    """The receipt projection paired with ``threads``, built once.

    Mirrors the thread store's own process cache: an in-process receipt
    beside a durable thread store — or two in-process receipt stores
    answering for the same one — would split a single turn across two
    histories, and a background resume that could not see the receipts
    its own door wrote would report a turn nobody else can find.
    """
    url = getattr(threads, "connection_url", None)
    durable_key = url if isinstance(url, str) else None
    # A stand-in that cannot be weak-referenced is not a history anyone
    # can share, so it is not cached at all rather than keyed by an
    # address the next object would inherit.
    cacheable = durable_key is not None or hasattr(threads, "__weakref__")
    with _PROJECTOR_LOCK:
        if not cacheable:
            return TurnReceiptProjector(default_receipt_store(threads))
        projector = (
            _DURABLE_PROJECTORS.get(durable_key)
            if durable_key is not None
            else _LOCAL_PROJECTORS.get(threads)
        )
        if projector is None:
            projector = TurnReceiptProjector(default_receipt_store(threads))
            if durable_key is not None:
                _DURABLE_PROJECTORS[durable_key] = projector
            else:
                _LOCAL_PROJECTORS[threads] = projector
        return projector


__all__ = [
    "TurnReceiptProjector",
    "cancellation_for",
    "default_receipts",
    "failure_token",
    "projected_stage_metrics",
]
