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
//
// The developer trace section is the panel's second surface: its geometry
// lives in ``traceWaterfall.ts`` and its receipt fallback rows in
// ``tracePanelView.ts``, so both branches stay unit-testable without a DOM.

import { TURN_RESULT_LABEL, type TaskStateView } from "../taskState";
import { readDeveloperModeEnabled } from "../../../devMode";
import { describeSearchModeReceipt } from "../../trace/searchModeReceipt";
import { receiptFallbackRows, traceReadHint } from "../tracePanelView";
import { traceIdText, type TraceSpan } from "../../trace/traceSpans";
import { traceWaterfall } from "../../trace/traceWaterfall";
import { useTurnTrace, type TurnTraceRead } from "../../trace/turnTraceRead";
import type { SearchModeDecision, Turn } from "../../../types";
import {
  gapsText,
  restrictionIntentText,
  runStatusText,
  sourcesText,
  staleIntentText,
  submissionText,
  taskOutcomeText,
  taskPanelSections,
} from "../taskPanelView";

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex gap-2 text-xs">
      <span className="shrink-0 text-gray-500">{label}</span>
      <span className="min-w-0 break-words">{value}</span>
    </div>
  );
}

interface TaskPanelProps {
  state: TaskStateView;
  devMode?: boolean;
  searchModeDecision?: SearchModeDecision | null;
  threadId?: string;
  /** The turn the developer trace section describes (the latest row). */
  currentTurn?: Turn | null;
  /** An injected read (tests / a parent prefetch); absent = fetch here. */
  trace?: TurnTraceRead;
}

export default function TaskPanel({
  state,
  devMode = readDeveloperModeEnabled(),
  searchModeDecision,
  threadId,
  currentTurn,
  trace,
}: TaskPanelProps) {
  // The trace read is a developer-only request: with the panel hidden or
  // no current turn it stays idle. The injected value wins so a render
  // test can pin the waterfall/fallback branches without the network.
  // ``settled`` is a dependency of the read (not just a render input):
  // the segment's spans are written when the turn ENDS, so the panel has
  // to read again when it does.
  const fetchedTrace = useTurnTrace({
    threadId,
    turnId: currentTurn?.run_id,
    enabled: devMode && trace === undefined,
    settled: currentTurn?.in_flight !== true,
  });

  // This panel is the INTERNAL receipt surface (specs/demo-seed.md item 6,
  // ADR 0100 decision 6): the run status, the Runtime submission receipt,
  // the answer-basis row and the source-evaluation row are accounting, not
  // answer. It is therefore hidden from the user AS A WHOLE by default —
  // not row by row — and renders in full only under developer mode. In
  // the default view the answer's own sources list is the only source
  // surface on the page; this one must not compete with it.
  //
  // The gate is on the RENDER alone. Every value below is still computed
  // by the pure ``taskPanelView`` functions, which stay exported and
  // unit-tested, so the facts remain derivable for the logs and nothing
  // is destroyed by hiding them. ``devMode`` is one boolean input to this
  // shell rather than a check scattered through each row.
  if (!devMode) return null;

  return (
    <TaskPanelBody
      state={state}
      searchModeDecision={searchModeDecision}
      threadId={threadId}
      currentTurn={currentTurn}
      read={trace ?? fetchedTrace}
      readInjected={trace !== undefined}
    />
  );
}

function TaskPanelBody({
  state,
  searchModeDecision,
  threadId,
  currentTurn,
  read,
  readInjected,
}: TaskPanelProps & { read: TurnTraceRead; readInjected: boolean }) {
  // A taskless thread with nothing pending renders nothing at all: an
  // empty task panel would imply a task exists and is simply unrecorded,
  // which is a different (and false) claim. Which sections survive that
  // rule is a pure decision (``taskPanelSections``), pinned without a DOM.
  const sections = taskPanelSections({
    showTaskPanel: state.showTaskPanel,
    hasSearchModeDecision: searchModeDecision !== undefined,
    hasTraceRead: readInjected,
    hasCurrentTurn: currentTurn != null,
  });
  if (sections === null) return null;

  return (
    <>
      {sections.showTask ? <TaskDetails state={state} /> : null}
      {sections.showSearchReceipt ? (
        <SearchModeReceipt decision={searchModeDecision ?? null} />
      ) : null}
      {sections.showTrace ? (
        <TurnTraceSection
          threadId={threadId ?? ""}
          turnId={currentTurn?.run_id ?? ""}
          state={state}
          currentTurn={currentTurn ?? null}
          read={read}
        />
      ) : null}
    </>
  );
}

// The developer trace section for the current turn. It has two honest
// branches: a waterfall when the turn has spans, and the receipt/status
// fallback plus a hint when it has none. An empty trace is never an error
// and never hides the receipt.
function TurnTraceSection({
  threadId,
  turnId,
  state,
  currentTurn,
  read,
}: {
  threadId: string;
  turnId: string;
  state: TaskStateView;
  currentTurn: Turn | null;
  read: TurnTraceRead;
}) {
  const spans = read.status === "ready" ? read.spans : [];
  return (
    <section
      data-testid="trace-panel"
      className="border border-black bg-tool px-3 py-2 space-y-1"
    >
      <p
        data-testid="trace-correlation"
        className="text-xs font-mono break-all text-gray-600"
      >
        线程 thread_id={threadId || "未记录"} · 轮次 turn_id=
        {turnId || "未记录"} · trace_id={traceIdText(spans)}
      </p>
      {spans.length > 0 ? (
        <TraceWaterfallRows spans={spans} />
      ) : (
        <TraceReceiptFallback
          state={state}
          currentTurn={currentTurn}
          hint={traceReadHint(read)}
        />
      )}
    </section>
  );
}

function TraceReceiptFallback({
  state,
  currentTurn,
  hint,
}: {
  state: TaskStateView;
  currentTurn: Turn | null;
  hint: string;
}) {
  return (
    <>
      <div data-testid="trace-receipt-fallback" className="space-y-1">
        {receiptFallbackRows(state, currentTurn).map((row) => (
          <div
            key={row.key}
            data-testid="trace-stage-row"
            data-stage={row.key}
            className="flex gap-2 text-xs"
          >
            <span className="shrink-0 text-gray-500">{row.label}</span>
            <span className="min-w-0 break-words">{row.value}</span>
          </div>
        ))}
      </div>
      <p
        data-testid="trace-disabled-hint"
        className="text-xs font-medium text-gray-600"
      >
        {hint}
      </p>
    </>
  );
}

function TraceWaterfallRows({
  spans,
}: {
  spans: readonly TraceSpan[];
}) {
  return (
    <div data-testid="trace-waterfall" className="space-y-1">
      {traceWaterfall(spans).map((row, index) => (
        <div
          key={`${row.span.span_id}-${index}`}
          data-testid="trace-span-row"
          data-span-name={row.span.name}
          data-left-percent={row.leftPercent}
          data-width-percent={row.widthPercent}
          className="space-y-0.5"
        >
          <div className="flex justify-between gap-2 text-xs">
            <span className="min-w-0 truncate font-mono">{row.span.name}</span>
            <span
              data-testid="trace-span-duration"
              className="shrink-0 font-mono text-gray-500"
            >
              {row.durationLabel}
            </span>
          </div>
          <div className="relative h-2 overflow-hidden bg-page">
            <div
              data-testid="trace-span-bar"
              className="absolute inset-y-0 bg-accent"
              style={{
                left: `${row.leftPercent}%`,
                width: `${row.widthPercent}%`,
              }}
            />
          </div>
        </div>
      ))}
    </div>
  );
}

function TaskDetails({ state }: { state: TaskStateView }) {
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

function SearchModeReceipt({
  decision,
}: {
  decision: SearchModeDecision | null;
}) {
  return (
    <section
      data-testid="search-mode-receipt"
      // PROVENANCE, not the retrieval implementation: the raw 档 token
      // must not reach the DOM in an attribute either, so the attribute
      // carries the decision tier (who decided) instead of the mode
      // (which implementation ran).
      data-decision-tier={decision?.decision_tier ?? "none"}
      className="border border-black bg-tool px-3 py-2"
    >
      <Row label="检索档" value={describeSearchModeReceipt(decision)} />
    </section>
  );
}
