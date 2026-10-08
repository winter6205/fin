// search-mode-host T2 fix: ONE constructor for the live / in-flight
// turn row. The optimistic row ``sendQuestion`` streams into and the
// row ``pickChoice`` materializes from a reloaded
// ``ThreadDetail.pending_turn`` are the SAME shape; building it in one
// place stops the two sites from drifting (the pick path had already
// lost ``status_classified`` / ``comparisons`` / ``visual_claims`` /
// ``trace_items``). Extracted to a pure module so the shape is
// unit-testable without Zustand, mirroring turnTrace / turnStream.

import { thinkingPendingItems } from "./turnTrace";
import type { ChoiceWindow, Turn } from "./types";

/**
 * Build the live optimistic turn a stream paints into: empty answer
 * slots, the in-flight flag, and the pending-thinking chrome. A
 * ``choice`` is present only on the pick path, whose freshly
 * materialized row starts owning the window it was waiting on (the
 * live-send row has none until the host emits a ``choice_window``
 * frame).
 */
export function makeLiveTurn(input: {
  runId: Turn["run_id"];
  question: string;
  choice?: ChoiceWindow;
}): Turn {
  return {
    run_id: input.runId,
    question: input.question,
    status: "done",
    status_reason: "",
    answer: "",
    citations: [],
    // E6: defined-empty until the ``evidence_refusals`` frame or a
    // decode refusal lands — the indicator renders from this field on
    // both doors (stream and GET history), so a reloaded turn shows
    // the same named codes it showed live.
    evidence_refusals: [],
    thinking_text: "",
    thinking_duration_ms: 0,
    tool_summaries: [],
    assistant_message: "",
    // Mid-flight stream error taxonomy: empty until the terminal
    // ``status`` SSE frame arrives; the SPA renders the "原因" label
    // off this field on refuse rows.
    status_classified: "",
    // T3 (enterprise-kb Focus 1): filled by ``comparison`` frames as
    // each ``compare_facts`` call returns, so the table is on screen
    // while the answer is still being authored.
    comparisons: [],
    // The run's visual-claim rows, filled by the ``visual_claims`` SSE
    // frame. Deliberately NOT part of ``citations`` — a generated
    // visual observation has no quote to place in the answer.
    visual_claims: [],
    // T4 (#37): the optimistic turn is LIVE until the terminal status
    // frame — the thinking region line-clamps and the tool rows stay
    // open while the run streams, then collapse.
    in_flight: true,
    // thinking pending: same 深度思考 chrome until the first thinking
    // frame (or drop on first tool_call / answer).
    trace_items: thinkingPendingItems(),
    created_at: new Date().toISOString(),
    ...(input.choice ? { choice_window: input.choice } : {}),
  };
}
