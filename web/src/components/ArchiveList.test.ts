// What the archive section renders about a restore — and, just as
// importantly, what it must NOT render. Every verdict on this surface
// comes from the ONE shared decision (``restoreDecision``), because a
// restore's answer is recorded on that restore's own request: it is not read
// back from whichever operation happens to be current on the thread (a
// message, a clarification reply, or a restore), so no later door can add,
// replace, or erase what this section says about the restore the operator
// actually asked for. A refused envelope is equally not a restore outcome:
// calling a decode problem a failed restore would assert a recheck that
// never happened.
//
// The dynamic state arrives as PROPS and the decision carries the thread it
// was read from: a verdict recorded on thread A must not describe thread B
// (the request record names its own thread and the view asks before it
// renders, so one thread's outcome cannot be painted onto another).
//
// Rendered through the prop-driven ``ArchiveListView`` with
// ``renderToString`` because this project's vitest setup has no DOM
// testing library (same split as VisualClaims.test.ts).

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { ArchiveListView } from "./ArchiveList";
import {
  RESTORE_ADOPTED_COPY,
  RESTORE_AWAITING_COPY,
  RESTORE_UNHEARD_COPY,
  RESTORE_UNRESOLVED_COPY,
  archiveRows,
  restoreDecision,
  restoreRefusalCopy,
  type RestoreDecision,
} from "../archiveRestore";
import type { ArchivedTaskSummary } from "../types";
import { TASK_RUNTIME_REFUSAL_COPY } from "../capabilities/task/taskRuntime";

const THREAD_A = "11111111-1111-4111-8111-111111111111";
const THREAD_B = "22222222-2222-4222-8222-222222222222";

const ARCHIVE_ID = "archive-task-1-v1";
const TASK_ID = "task-1";

function summary(
  overrides: Partial<ArchivedTaskSummary> = {},
): ArchivedTaskSummary {
  return {
    archive_id: ARCHIVE_ID,
    task_id: TASK_ID,
    goal: "比较营收",
    task_version: 1,
    archived_at_input_version: 2,
    run_id: null,
    evidence_refs: [],
    outcome: "active",
    ...overrides,
  };
}

/** The decision the page would hand down for one host state, so the view
 * is exercised through the same authority production uses. */
function decide(over: {
  threadId?: string;
  /** The listing the fresh projection reports NOW. */
  listed?: ArchivedTaskSummary[];
  /** The listing the archive was clicked from; adoption needs the row's
   * task id, which the fresh projection no longer carries once adopted. */
  offered?: ArchivedTaskSummary[];
  requested?: boolean;
  accepted?: boolean;
  failedToSend?: boolean;
  /** The verdict the host named for THIS restore, recorded on the request. */
  refusalKind?: string;
  currentTaskId?: string | null;
  fresh?: boolean;
} = {}): RestoreDecision {
  const listed = over.listed ?? [summary()];
  return restoreDecision({
    threadId: over.threadId ?? THREAD_A,
    request: over.requested
      ? {
          archiveId: ARCHIVE_ID,
          taskId: TASK_ID,
          offered: over.offered ?? [summary()],
          accepted: over.accepted ?? false,
          failedToSend: over.failedToSend ?? false,
          refusalKind: over.refusalKind ?? null,
        }
      : null,
    projection: {
      currentTaskId: over.currentTaskId === undefined ? "task-current" : over.currentTaskId,
      archived: listed,
      fresh: over.fresh ?? true,
    },
  });
}

function render(props: {
  threadId?: string;
  rows?: ReturnType<typeof archiveRows>;
  restorable?: string[];
  decision?: RestoreDecision | null;
  projectionRefusal?: "malformed_task_runtime" | "unsupported_task_runtime" | null;
} = {}): string {
  const listed = [summary()];
  return renderToString(
    createElement(ArchiveListView, {
      threadId: props.threadId ?? THREAD_A,
      rows: props.rows ?? archiveRows(listed),
      restorable: new Set(props.restorable ?? [ARCHIVE_ID]),
      decision: props.decision === undefined ? null : props.decision,
      projectionRefusal: props.projectionRefusal ?? null,
      onRestore: () => undefined,
    }),
  );
}

describe("a restore refusal renders restore copy", () => {
  it("shows the named restore failure the decision reported", () => {
    const html = render({
      decision: decide({ requested: true, refusalKind: "task_restore_not_adopted" }),
    });
    expect(html).toContain(restoreRefusalCopy("task_restore_not_adopted"));
    expect(html).toContain('data-testid="archive-refusal"');
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
  });

  it("falls back to generic copy for an unmapped kind", () => {
    const html = render({
      decision: decide({ requested: true, refusalKind: "some_future_restore_kind" }),
    });
    expect(html).toContain("这次恢复没有完成，当前任务保持不变。可以稍后重试。");
  });

  it("states an unheard request without claiming adoption", () => {
    const html = render({ decision: decide({ requested: true, failedToSend: true }) });
    expect(html).toContain(RESTORE_UNHEARD_COPY);
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
  });

  it("renders no restore refusal for a request the host has not answered", () => {
    // The refusal line comes only from what THIS restore was answered with,
    // so an open request states the confirmation it is owed and nothing
    // else — no refusal borrowed from another door's outcome.
    const html = render({ decision: decide({ requested: true, accepted: true }) });
    expect(html).not.toContain('data-testid="archive-refusal"');
    expect(html).toContain('data-testid="archive-awaiting"');
    expect(html).toContain(RESTORE_AWAITING_COPY);
    expect(html).toContain('data-testid="archive-row-archive-task-1-v1"');
  });
});

describe("an unresolved projection is visible and claims nothing", () => {
  it("renders the unresolved notice without an adoption claim", () => {
    // The store's refresh-failed state has no UI consumer today, so an
    // unreadable re-read must not read as "the host confirmed this".
    const html = render({
      decision: decide({ requested: true, accepted: true, fresh: false }),
    });
    expect(html).toContain('data-testid="archive-unresolved"');
    expect(html).toContain(RESTORE_UNRESOLVED_COPY);
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
    expect(html).not.toContain('data-testid="archive-restored"');
  });

  it("keeps the listed rows visible while the check is unresolved", () => {
    const html = render({
      decision: decide({ requested: true, accepted: true, fresh: false }),
    });
    expect(html).toContain('data-testid="archive-row-archive-task-1-v1"');
    expect(html).toContain("进行中");
  });
});

describe("a correlated confirmation is the only adoption claim", () => {
  it("names the archive adopted by the fresh projection", () => {
    const html = render({
      decision: decide({
        requested: true,
        accepted: true,
        listed: [],
        currentTaskId: TASK_ID,
      }),
      rows: archiveRows([]),
      restorable: [],
    });
    expect(html).toContain('data-testid="archive-restored"');
    expect(html).toContain(RESTORE_ADOPTED_COPY);
  });

  it("claims nothing from durable acceptance alone", () => {
    const html = render({ decision: decide({ requested: true, accepted: true }) });
    expect(html).not.toContain('data-testid="archive-restored"');
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
    expect(html).toContain('data-testid="archive-awaiting"');
  });

  it("claims nothing when the archive is still listed under the current task", () => {
    const html = render({
      decision: decide({ requested: true, accepted: true, currentTaskId: TASK_ID }),
    });
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
  });
});

describe("a verdict is scoped to the thread its facts were read from", () => {
  it("renders no restore verdict for another thread's decision", () => {
    const adoptedElsewhere = decide({
      requested: true,
      accepted: true,
      listed: [],
      currentTaskId: TASK_ID,
    });
    const html = render({
      threadId: THREAD_B,
      rows: archiveRows([summary({ archive_id: "archive-task-9-v1", task_id: "task-9" })]),
      restorable: ["archive-task-9-v1"],
      decision: adoptedElsewhere,
    });
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
    expect(html).not.toContain('data-testid="archive-restored"');
  });

  it("suppresses an in-flight and a refused verdict across threads", () => {
    const pendingA = decide({ requested: true });
    const refusedA = decide({ requested: true, refusalKind: "task_restore_not_adopted" });
    const pending = render({ threadId: THREAD_B, decision: pendingA });
    const refused = render({ threadId: THREAD_B, decision: refusedA });
    expect(pending).not.toContain("复核中…");
    expect(pending.split("disabled=").length - 1).toBe(0);
    expect(refused).not.toContain('data-testid="archive-refusal"');
    // The same decisions on their own thread do render.
    expect(render({ threadId: THREAD_A, decision: pendingA })).toContain("复核中…");
    expect(render({ threadId: THREAD_A, decision: refusedA })).toContain(
      'data-testid="archive-refusal"',
    );
  });
});

describe("a refused task envelope is stated, not silent", () => {
  it("renders the decode-refusal copy even with no archive rows", () => {
    // A malformed envelope decodes to the empty view: with no record of
    // the refusal, this section would collapse to nothing and the
    // operator would read "no tasks" off a BROKEN channel.
    const html = render({ projectionRefusal: "malformed_task_runtime" });
    expect(html).toContain('data-testid="archive-projection-refusal"');
    expect(html).toContain(TASK_RUNTIME_REFUSAL_COPY.malformed_task_runtime);
  });

  it("renders the unsupported-envelope copy with its own wording", () => {
    const html = render({ projectionRefusal: "unsupported_task_runtime" });
    expect(html).toContain(TASK_RUNTIME_REFUSAL_COPY.unsupported_task_runtime);
    expect(html).not.toContain(TASK_RUNTIME_REFUSAL_COPY.malformed_task_runtime);
  });

  it("keeps the refusal visible alongside listed rows", () => {
    const html = render({ projectionRefusal: "malformed_task_runtime" });
    expect(html).toContain('data-testid="archive-row-archive-task-1-v1"');
    expect(html).toContain(TASK_RUNTIME_REFUSAL_COPY.malformed_task_runtime);
  });

  it("stays collapsed for a healthy taskless surface", () => {
    // No rows, no refusal, nothing owed: the section renders nothing at
    // all — the decision must not invent a notice for a clean empty state.
    expect(render({ rows: [], restorable: [], decision: decide() })).toBe("");
    expect(render({ rows: [], restorable: [], decision: null })).toBe("");
  });

  it("does not render the restore-refusal line for a decode refusal", () => {
    const html = render({ projectionRefusal: "malformed_task_runtime" });
    expect(html).not.toContain('data-testid="archive-refusal"');
  });

  it("renders both refusals apart when both are present", () => {
    const html = render({
      decision: decide({ requested: true, refusalKind: "task_restore_not_adopted" }),
      projectionRefusal: "malformed_task_runtime",
    });
    expect(html).toContain('data-testid="archive-refusal"');
    expect(html).toContain('data-testid="archive-projection-refusal"');
    expect(html).toContain(restoreRefusalCopy("task_restore_not_adopted"));
    expect(html).toContain(TASK_RUNTIME_REFUSAL_COPY.malformed_task_runtime);
  });
});

describe("the restore action stays explicit", () => {
  it("arms exactly the clicked archive while the check is owed", () => {
    const html = render({
      rows: archiveRows([summary(), summary({ archive_id: "archive-task-2-v1" })]),
      restorable: [ARCHIVE_ID, "archive-task-2-v1"],
      decision: decide({ requested: true }),
    });
    // The re-checking row shows its pending copy and every button is
    // disabled while one restore is in flight.
    expect(html).toContain("复核中…");
    expect(html.split("disabled=").length - 1).toBe(2);
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
  });

  it("re-arms every restore action as soon as the host has answered", () => {
    // An accepted restore whose adoption Runtime has not yet proven still
    // owes a confirmation, but it must not park the whole listing: the
    // operator can still act on the other archived tasks.
    const html = render({
      rows: archiveRows([summary(), summary({ archive_id: "archive-task-2-v1" })]),
      restorable: [ARCHIVE_ID, "archive-task-2-v1"],
      decision: decide({ requested: true, accepted: true }),
    });
    expect(html.split("disabled=").length - 1).toBe(0);
    expect(html).toContain('data-testid="archive-awaiting"');
    expect(html).not.toContain(RESTORE_ADOPTED_COPY);
  });

  it("re-arms the actions after a named refusal and after an unheard send", () => {
    const refused = render({
      decision: decide({ requested: true, refusalKind: "task_restore_not_adopted" }),
    });
    const unheard = render({ decision: decide({ requested: true, failedToSend: true }) });
    expect(refused).toContain('data-testid="archive-refusal"');
    expect(refused.split("disabled=").length - 1).toBe(0);
    expect(unheard).toContain(RESTORE_UNHEARD_COPY);
    expect(unheard.split("disabled=").length - 1).toBe(0);
  });

  it("gives only listed non-blank ids a button", () => {
    const rows = archiveRows([
      summary({ archive_id: "" }),
      summary({ archive_id: ARCHIVE_ID }),
    ]);
    const html = render({ rows, restorable: [ARCHIVE_ID] });
    expect(html).toContain(`data-testid="archive-restore-${ARCHIVE_ID}"`);
    expect(html).not.toContain('data-testid="archive-restore-"');
  });

  it("keeps the archived outcome as the outcome at archive time", () => {
    const html = render({ rows: archiveRows([summary({ outcome: "complete" })]) });
    expect(html).toContain("已完成");
    expect(html).not.toContain("进行中");
  });
});
