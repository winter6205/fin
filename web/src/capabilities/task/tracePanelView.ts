// The TaskPanel trace section's non-waterfall facts: the receipt fallback
// rows and the hint that says why no waterfall is drawn. Kept pure so both
// branches are pinned without a DOM, and so the panel cannot quietly claim
// a trace ran when the turn only has its receipt/status data.
//
// The fallback reuses the turn facts the thread already holds — the run
// status / turn result / turn status from ``TaskStateView`` and the
// persisted thinking / tool stages on the turn — rather than inventing a
// stage list or a second label map. The span list is the trace layer's own
// fact; when it is empty the receipt is what remains, and the hint says why.

import { TURN_RESULT_LABEL, turnStatusLabel, type TaskStateView } from "./taskState";
import { runStatusText } from "./taskPanelView";
import { traceItemsFromTurn } from "../../turnTrace";
import type { TraceState } from "../trace/traceSpans";
import type { TurnTraceRead } from "../trace/turnTraceRead";
import type { Turn } from "../../types";
import { formatDuration } from "../trace/traceWaterfall";

export interface TraceReceiptRow {
  key: string;
  label: string;
  value: string;
}

function toolStageText(calls: readonly { name: string }[]): string {
  const names: string[] = [];
  for (const call of calls) {
    if (!names.includes(call.name)) names.push(call.name);
  }
  const label = `${calls.length} 次`;
  return names.length > 0 ? `${label}：${names.join("、")}` : label;
}

/** The receipt facts shown beside the "no waterfall" hint: status first,
 * then the stages the turn actually recorded. A turn with no stage entry
 * says so rather than rendering an empty list that reads as "nothing
 * happened". */
export function receiptFallbackRows(
  state: TaskStateView,
  turn: Turn | null,
): TraceReceiptRow[] {
  const rows: TraceReceiptRow[] = [
    { key: "run-status", label: "运行", value: runStatusText(state) },
    { key: "turn-result", label: "本轮", value: TURN_RESULT_LABEL[state.turnResult] },
  ];
  if (turn === null) {
    rows.push({ key: "turn-status", label: "轮次", value: "没有当前轮次" });
    return rows;
  }

  rows.push({ key: "turn-status", label: "轮次", value: turnStatusLabel(turn) });
  const stages = traceItemsFromTurn(turn);
  if (stages.length === 0) {
    rows.push({ key: "stages", label: "阶段", value: "回执未记录阶段" });
    return rows;
  }
  for (const [index, item] of stages.entries()) {
    if (item.kind === "thinking") {
      const duration = turn.thinking_duration_ms;
      rows.push({
        key: `thinking-${index}`,
        label: "思考",
        value:
          typeof duration === "number" && duration > 0
            ? formatDuration(duration)
            : "已记录",
      });
    } else {
      rows.push({
        key: `tools-${index}`,
        label: "工具",
        value: toolStageText(item.calls),
      });
    }
  }
  return rows;
}

/** Why no waterfall is drawn, one sentence per state. ``null`` is "the view
 * could not read the exporter state", which is stated as unknown rather
 * than as disabled. */
export function traceHintText(traceState: TraceState | null): string {
  if (traceState === "off") return "trace 未开启";
  if (traceState === "local") return "trace 已开启，但本轮没有 span";
  if (traceState === "external") return "trace 已开启（非本地存储）";
  return "trace 状态不可读，显示回执";
}

/** The hint for ONE read: the loading state is its own sentence, and a
 * failed read states the trace as unreadable rather than disabled. */
export function traceReadHint(read: TurnTraceRead): string {
  if (read.status === "ready") return traceHintText(read.traceState);
  if (read.status === "error") return traceHintText(null);
  return "trace 状态读取中…";
}
