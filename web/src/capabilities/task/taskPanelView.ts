// question-workspace W1: the TaskPanel row VALUES as pure functions.
//
// WHY a module and not inline JSX: the web suite runs without a DOM and
// never collects ``.tsx`` (same split as ``taskState.ts``), so the
// branchy render decisions — what the task/run/gap/source rows say — must
// live in pure code to be pinned by tests. ``TaskPanel.tsx`` only maps
// these strings onto ``<Row>``s.
//
// The rules encoded here (spec SC-1/SC-2, contract §Ownership): TASK
// outcome, TURN result and RUN status are three separate facts; an ABSENT
// gap report is stated as such; a saved source restriction is user INTENT
// and never shown as a grant; an unknown projection is stated as unknown,
// never as a completion.

import type { SourceEvaluationReason, TaskStateView } from "./taskState";
import {
  RUN_STATUS_LABEL,
  SUBMISSION_STATE_LABEL,
  TASK_OUTCOME_LABEL,
} from "./taskState";

/** Why no source evaluation is shown, stated per reason. Typed against the
 * whole union so a new reason cannot silently render an empty row — an
 * empty value would read as "nothing to report" rather than as a refusal. */
const SOURCE_UNAVAILABLE_COPY: Record<SourceEvaluationReason, string> = {
  evaluation_unavailable:
    "已保存的来源限制还没有当前授权，这次不显示任何可用来源。",
  not_evaluated: "当前没有可显示的来源评估。",
};

/** The 「任务」 row: the envelope's outcome verbatim; an explicit "no task"
 * on a taskless thread; and an explicit "unreadable" when the projection
 * could not be read. Never synthesized from a turn result, and never
 * worded as a completion by an unknown state. */
export function taskOutcomeText(state: TaskStateView): string {
  if (state.taskOutcome !== null) return TASK_OUTCOME_LABEL[state.taskOutcome];
  return state.projectionUnreadable ? "未记录（投影无法读取）" : "尚无任务";
}

/** The 「运行」 row: the run's own status, a different fact from the turn
 * result and from the task outcome (spec §"Active work, outcomes, and
 * history" keeps them distinct). */
export function runStatusText(state: TaskStateView): string {
  return RUN_STATUS_LABEL[state.runStatus];
}

/** The 「提交」 row: the notice its owner supplied, or nothing when there is
 * none. An empty text still renders the state label, never a blank row. */
export function submissionText(state: TaskStateView): string | null {
  const notice = state.submission ?? null;
  if (notice === null) return null;
  const label = SUBMISSION_STATE_LABEL[notice.state];
  const text = notice.text.trim();
  return text.length > 0 ? `${label}：${text}` : label;
}

/** The 「待补」 row: an ABSENT report is stated as such. Reporting an
 * empty list would claim the task has no unresolved gaps, which is not
 * what silence means (contract §Ownership). */
export function gapsText(state: TaskStateView): string {
  if (!state.gaps.reported) return "Runtime 尚未报告";
  return state.gaps.gaps.length > 0 ? state.gaps.gaps.join("；") : "无";
}

/** The 「来源」 row, available branch: the effective scope plus the
 * operator's executable capabilities, each stated even when empty. */
function availableSourcesText(state: TaskStateView): string {
  const effective =
    state.sources.available && state.sources.effectiveSourceIds.length > 0
      ? state.sources.effectiveSourceIds.join("、")
      : "当前没有可用来源";
  const capabilities =
    state.sources.available && state.sources.capabilities.length > 0
      ? `能力：${state.sources.capabilities.join("、")}`
      : "当前没有可执行的操作";
  return [effective, capabilities].join("；");
}

/** The 「来源」 row: a saved restriction is user INTENT and never shown
 * as a grant; without a fresh evaluation no source is offered as
 * available at all (SC-2). */
export function sourcesText(state: TaskStateView): string {
  return state.sources.available
    ? availableSourcesText(state)
    : SOURCE_UNAVAILABLE_COPY[state.sources.reason];
}

/** The 「来源限制」 row: the saved restriction of a FRESH evaluation, always
 * annotated as intent and never merged into the effective grant.
 *
 * With no evaluation there is nothing to annotate: ``SourceEvaluation``
 * carries the intent only on its available variant, because a restriction
 * the host never evaluated has no row here — the panel states the
 * evaluation's absence (``sourcesText``), not a second, unevaluated copy of
 * a string the host did not project. */
export function restrictionIntentText(state: TaskStateView): string | null {
  if (!state.sources.available) return null;
  const intent = state.sources.restrictionIntent;
  return intent.length > 0 ? `${intent}（用户意图，不代表当前授权）` : null;
}

/** The 「已失效」 row: intent ids the fresh evaluation did not grant, so
 * the operator sees which saved sources are NOT currently authorized. */
export function staleIntentText(state: TaskStateView): string | null {
  return state.sources.available && state.sources.staleIntentIds.length > 0
    ? `${state.sources.staleIntentIds.join("、")}（当前权限下不可用）`
    : null;
}

/** Which sections the panel renders at all.
 *
 * A taskless thread with nothing pending renders nothing — an empty task
 * receipt would imply a task exists and is simply unrecorded, which is a
 * different (and false) claim. The search-mode receipt and the trace
 * section have their own reasons to render, so either one keeps the panel
 * alive. ``null`` is "render nothing".
 */
export function taskPanelSections(input: {
  showTaskPanel: boolean;
  hasSearchModeDecision: boolean;
  hasTraceRead: boolean;
  hasCurrentTurn: boolean;
}): {
  showTask: boolean;
  showSearchReceipt: boolean;
  showTrace: boolean;
} | null {
  const showSearchReceipt = input.hasSearchModeDecision;
  const showTrace = input.hasTraceRead || input.hasCurrentTurn;
  if (!input.showTaskPanel && !showSearchReceipt && !showTrace) return null;
  return { showTask: input.showTaskPanel, showSearchReceipt, showTrace };
}
