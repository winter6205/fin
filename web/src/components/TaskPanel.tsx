// question-workspace W1: the current task, its sources, and its outcome
// (specs/question-workspace.md §"State shown to the operator"; SC-1, SC-2).
//
// WHY this is a thin shell over ``taskPanelView.ts``: the judgement —
// which turn result to show, what each row says, which conditional rows
// render — is pure and unit-tested there, because the web suite runs
// without a DOM and never collects ``.tsx``. This file only maps the
// computed values onto ``<Row>``s.
//
// The one rule the markup enforces structurally: task outcome, turn result
// and run status are separate rows, each fed by its own owner. A run that
// ended, a citation that appeared, or a lookup that succeeded must never
// make this panel read as "the task is done", so the task outcome is only
// ever the string ``taskState`` derived from the authoritative envelope.

import { TURN_RESULT_LABEL, type TaskStateView } from "../taskState";
import {
  gapsText,
  restrictionIntentText,
  runStatusText,
  sourcesText,
  staleIntentText,
  submissionText,
  taskOutcomeText,
} from "../taskPanelView";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="shrink-0 text-gray-500">{label}</span>
      <span className="min-w-0 break-words">{value}</span>
    </div>
  );
}

export default function TaskPanel({ state }: { state: TaskStateView }) {
  // A taskless thread with nothing pending renders nothing at all: an
  // empty task panel would imply a task exists and is simply unrecorded,
  // which is a different (and false) claim.
  if (!state.showTaskPanel) return null;

  const restriction = restrictionIntentText(state);
  const stale = staleIntentText(state);
  const submission = submissionText(state);

  return (
    <section
      data-testid="task-panel"
      className="border border-black bg-tool px-3 py-2 space-y-1"
    >
      {/* Task outcome, turn result and run status are three rows because
          they are three facts owned by three different parties: Runtime's
          envelope, what this turn produced, and what the run reported. A
          run that ended, a citation that appeared, or a lookup that
          succeeded must never make this panel read as "the task is done",
          so the task outcome is only ever the string ``taskState`` derived
          from the authoritative envelope. */}
      <Row label="任务" value={taskOutcomeText(state)} />
      <Row label="本轮" value={TURN_RESULT_LABEL[state.turnResult]} />
      <Row label="运行" value={runStatusText(state)} />

      {/* The submission's own outcome, its owner's wording. Absent when
          nothing was submitted, so the panel never claims one was. */}
      {submission ? <Row label="提交" value={submission} /> : null}

      {state.goal ? <Row label="目标" value={state.goal} /> : null}
      {state.expectedResult ? (
        <Row label="期望结果" value={state.expectedResult} />
      ) : null}
      {state.taskOutcomeReason ? (
        <Row label="说明" value={state.taskOutcomeReason} />
      ) : null}

      {/* Gaps: an ABSENT report is stated as such; the wording lives in
          taskPanelView.gapsText. */}
      <Row label="待补" value={gapsText(state)} />

      {/* Sources: a saved restriction is user INTENT and never shown as
          a grant; without a fresh evaluation no source is offered as
          available at all (SC-2). Read-only by construction — the panel
          renders text, never a control, so it cannot offer a
          retrieval-technique picker or an inaccessible source. */}
      <Row label="来源" value={sourcesText(state)} />
      {restriction ? <Row label="来源限制" value={restriction} /> : null}
      {stale ? <Row label="已失效" value={stale} /> : null}
    </section>
  );
}
