import { describe, expect, it } from "vitest";
import {
  ARCHIVE_HISTORY_NOTICE,
  RESTORE_ADOPTED_COPY,
  RESTORE_AWAITING_COPY,
  RESTORE_UNHEARD_COPY,
  RESTORE_UNRESOLVED_COPY,
  archiveOutcomeLabel,
  archiveRows,
  restorableArchiveIds,
  restoreDecision,
  restoreIsListed,
  restoreRefusalCopy,
  type RestoreDecisionInput,
  type RestoreProjectionRef,
  type RestoreRequestRef,
} from "./archiveRestore";
import type { ArchivedTaskSummary } from "./types";

const THREAD_A = "11111111-1111-4111-8111-111111111111";
const THREAD_B = "22222222-2222-4222-8222-222222222222";

function summary(overrides: Partial<ArchivedTaskSummary> = {}): ArchivedTaskSummary {
  return {
    archive_id: "arch-1",
    task_id: "task-1",
    goal: "goal",
    task_version: 1,
    archived_at_input_version: 2,
    run_id: null,
    evidence_refs: [],
    ...overrides,
  };
}

/** A readable fresh projection. ``archived`` still lists ``arch-1`` and
 * ``currentTaskId`` is NOT the archived task, so the default describes a
 * thread where nothing has been adopted. */
function projection(
  overrides: Partial<RestoreProjectionRef> = {},
): RestoreProjectionRef {
  return {
    currentTaskId: "task-current",
    archived: [summary()],
    fresh: true,
    ...overrides,
  };
}

/** A restore the operator explicitly clicked: archive ``arch-1``, whose
 * row carried task id ``task-1`` in the listing it was picked from. The host
 * has answered nothing about it yet. */
function request(overrides: Partial<RestoreRequestRef> = {}): RestoreRequestRef {
  return {
    archiveId: "arch-1",
    taskId: "task-1",
    offered: [summary()],
    accepted: false,
    failedToSend: false,
    refusalKind: null,
    ...overrides,
  };
}

function decide(input: Partial<RestoreDecisionInput> = {}) {
  return restoreDecision({
    threadId: THREAD_A,
    request: null,
    projection: projection(),
    ...input,
  });
}

describe("archiveRows", () => {
  it("keeps the host's newest-first order unchanged", () => {
    const rows = archiveRows([
      summary({ archive_id: "a", goal: "first" }),
      summary({ archive_id: "b", goal: "second" }),
    ]);
    expect(rows.map((row) => row.archiveId)).toEqual(["a", "b"]);
  });

  it("yields [] for an older host with no archive projection", () => {
    expect(archiveRows(undefined)).toEqual([]);
  });

  it("carries the row's own task id, which adoption is proven against", () => {
    const rows = archiveRows([summary({ archive_id: "a", task_id: "task-a" })]);
    expect(rows[0].taskId).toBe("task-a");
    expect(rows[0].archiveId).toBe("a");
  });

  it("renders a blank goal as empty rather than synthesising a description", () => {
    expect(archiveRows([summary({ goal: "   " })])[0].goal).toBe("");
  });

  it("keeps the archived outcome even when it is 'active': archiving is not completion", () => {
    const rows = archiveRows([summary({ outcome: "active" })]);
    expect(rows[0].outcomeAtArchive).toBe("active");
  });

  it("reads 未记录 when the host does not project an archive outcome", () => {
    expect(archiveRows([summary()])[0].outcomeAtArchive).toBeNull();
  });
});

describe("restoreDecision (SC-6: adoption needs the correlated fresh projection)", () => {
  it("is idle with no request, no proof, and no refusal", () => {
    const view = decide();
    expect(view.state).toBe("idle");
    expect(view.notice).toBeNull();
    expect(view.currentTaskId).toBe("task-current");
    expect(view.restoredArchiveId).toBeNull();
    expect(view.projectionUnresolved).toBe(false);
  });

  it("never reports restored from the click alone", () => {
    const view = decide({ request: request() });
    expect(view.state).not.toBe("restored");
    expect(view.restoredArchiveId).toBeNull();
    expect(view.awaitingConfirmation).toBe(true);
    expect(view.pendingArchiveId).toBe("arch-1");
    expect(view.currentTaskId).toBe("task-current");
  });

  it("treats a durable acceptance as confirmation still owed, never adoption", () => {
    // The store's real post-send facts: the request is recorded and
    // accepted, the projection still names the old current task and still
    // lists the archive. Acceptance must not become adoption.
    const view = decide({ request: request({ accepted: true }) });
    expect(view.state).toBe("awaiting_confirmation");
    expect(view.restoredArchiveId).toBeNull();
    expect(view.notice).toBe(RESTORE_AWAITING_COPY);
    expect(view.notice).not.toBe(RESTORE_ADOPTED_COPY);
  });

  it("adopts only when the fresh projection reports the clicked row's task AND drops that archive", () => {
    const view = decide({
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-1", archived: [] }),
    });
    expect(view.state).toBe("restored");
    expect(view.restoredArchiveId).toBe("arch-1");
    expect(view.currentTaskId).toBe("task-1");
    expect(view.awaitingConfirmation).toBe(false);
    expect(view.notice).toBe(RESTORE_ADOPTED_COPY);
  });

  it("does not adopt when the current task matches but the archive is still listed", () => {
    // The row was not removed, so the host did not adopt it: the id the
    // operator picked is still offered for another restore.
    const view = decide({
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-1" }),
    });
    expect(view.state).toBe("awaiting_confirmation");
    expect(view.restoredArchiveId).toBeNull();
  });

  it("does not adopt when the archive left the list but another task is current", () => {
    // Absence alone proves nothing: the ids must correspond to the SAME
    // archived task the operator picked.
    const view = decide({
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-other", archived: [] }),
    });
    expect(view.state).toBe("awaiting_confirmation");
    expect(view.restoredArchiveId).toBeNull();
  });

  it("cannot mark a second archive restored from the first archive's proof", () => {
    // Two archives, one restore, asked about twice: arch-1 became current
    // and left the list; arch-2 is still listed with its own task id.
    const archTwo = summary({ archive_id: "arch-2", task_id: "task-2" });
    const adopted = projection({ currentTaskId: "task-1", archived: [archTwo] });
    const first = decide({
      request: request({ accepted: true }),
      projection: adopted,
    });
    const second = decide({
      request: request({ archiveId: "arch-2", taskId: "task-2", offered: [summary(), archTwo], accepted: true }),
      projection: adopted,
    });
    expect(first.restoredArchiveId).toBe("arch-1");
    expect(second.state).not.toBe("restored");
    expect(second.restoredArchiveId).toBeNull();
    expect(second.notice).toBe(RESTORE_AWAITING_COPY);
  });

  it("lets a refusal for the current request outrank an older correlated confirmation", () => {
    const view = decide({
      request: request({
        accepted: true,
        refusalKind: "task_restore_not_adopted",
      }),
      projection: projection({ currentTaskId: "task-1", archived: [] }),
    });
    expect(view.state).toBe("refused");
    expect(view.restoredArchiveId).toBeNull();
    expect(view.currentTaskId).toBe("task-1");
    expect(view.notice).toBe(restoreRefusalCopy("task_restore_not_adopted"));
  });

  it("states a refusal only from what the restore itself was answered", () => {
    // The thread's current operation is not an input to this decision at
    // all: whichever door the operator opened last, the only refusal this
    // surface may state is the one recorded on this restore's request.
    const owed = decide({ request: request() });
    expect(owed.state).toBe("awaiting_confirmation");
    expect(owed.notice).toBe(RESTORE_AWAITING_COPY);
    expect(owed.requestUnsettled).toBe(true);
  });

  it("refuses a request naming no offered archive", () => {
    const blank = decide({ request: request({ archiveId: "   " }) });
    const unlisted = decide({ request: request({ archiveId: "arch-gone" }) });
    expect(blank.state).toBe("refused");
    expect(blank.notice).toBe(restoreRefusalCopy("task_restore_unknown"));
    expect(unlisted.state).toBe("refused");
    expect(unlisted.currentTaskId).toBe("task-current");
    expect(unlisted.restoredArchiveId).toBeNull();
  });

  it("refuses a request Runtime could not hear instead of leaving it pending", () => {
    const view = decide({ request: request({ failedToSend: true }) });
    expect(view.state).toBe("refused");
    expect(view.notice).toBe(RESTORE_UNHEARD_COPY);
    expect(view.awaitingConfirmation).toBe(false);
    expect(view.restoredArchiveId).toBeNull();
  });

  it("stays unresolved when the last projection re-read could not be read", () => {
    // ``fresh: false`` is the store's refresh-failed / refused-envelope
    // state: the ids below are the LAST READABLE facts, so the existing
    // current task keeps rendering but nothing may be confirmed.
    const view = decide({
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-1", archived: [], fresh: false }),
    });
    expect(view.state).toBe("unresolved");
    expect(view.projectionUnresolved).toBe(true);
    expect(view.restoredArchiveId).toBeNull();
    expect(view.notice).toBe(RESTORE_UNRESOLVED_COPY);
    expect(view.currentTaskId).toBe("task-1");
    expect(view.notice).not.toBe(RESTORE_ADOPTED_COPY);
  });

  it("stays unresolved while there is an open request and no projection at all", () => {
    const view = decide({ request: request(), projection: null });
    expect(view.state).toBe("unresolved");
    expect(view.currentTaskId).toBeNull();
  });

  it("claims nothing about a restore for a thread that asked for none, even with an unreadable projection", () => {
    // An unreadable projection is a broken CHANNEL, not a restore outcome:
    // it is stated by the projection-refusal notice, and this section may
    // not name a restore the operator never requested — not even to say the
    // request could not be confirmed. The last readable ids still render.
    const view = decide({ projection: projection({ fresh: false }) });
    expect(view.state).toBe("idle");
    expect(view.notice).toBeNull();
    expect(view.restoredArchiveId).toBeNull();
    expect(view.projectionUnresolved).toBe(false);
    expect(view.awaitingConfirmation).toBe(false);
    expect(view.requestUnsettled).toBe(false);
    expect(view.currentTaskId).toBe("task-current");
  });

  it("names the thread its facts were read from", () => {
    const adopted = projection({ currentTaskId: "task-1", archived: [] });
    const inA = decide({ threadId: THREAD_A, request: request({ accepted: true }), projection: adopted });
    // Thread B lists arch-1 under a different task id, so the same
    // current-task id proves nothing there.
    const inB = decide({
      threadId: THREAD_B,
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-1", archived: [summary({ task_id: "task-9" })] }),
    });
    expect(inA.threadId).toBe(THREAD_A);
    expect(inB.threadId).toBe(THREAD_B);
    expect(inA.restoredArchiveId).toBe("arch-1");
    expect(inB.restoredArchiveId).toBeNull();
  });
});

describe("restoreDecision (the window in which the listing holds still)", () => {
  it("keeps the confirmation owed and the current task visible while an accepted restore is unproven", () => {
    // Runtime has recorded the request and has not yet made the archive
    // current: the check is still owed, so the existing task stays on screen
    // and the surface says "still owed" rather than "restored".
    const view = decide({
      request: request({ accepted: true }),
      projection: projection({ currentTaskId: "task-current" }),
    });
    expect(view.state).toBe("awaiting_confirmation");
    expect(view.awaitingConfirmation).toBe(true);
    expect(view.notice).toBe(RESTORE_AWAITING_COPY);
    expect(view.currentTaskId).toBe("task-current");
    expect(view.restoredArchiveId).toBeNull();
    // The host HAS answered this restore, so the request no longer holds
    // the rest of the listing still.
    expect(view.requestUnsettled).toBe(false);
  });

  it("holds the listing only while the request has no answer at all", () => {
    const justClicked = decide({ request: request() });
    expect(justClicked.requestUnsettled).toBe(true);
    expect(decide({ request: request({ accepted: true }) }).requestUnsettled).toBe(
      false,
    );
    expect(
      decide({ request: request({ failedToSend: true }) }).requestUnsettled,
    ).toBe(false);
    expect(
      decide({
        request: request({ refusalKind: "task_permission_denied" }),
      }).requestUnsettled,
    ).toBe(false);
    // An id the listing never offered is answered by the listing check
    // itself, so it is no more in flight than a host refusal.
    expect(
      decide({ request: request({ archiveId: "arch-gone" }) }).requestUnsettled,
    ).toBe(false);
  });
});

describe("restoreDecision history notice", () => {
  it("is stated whenever archives exist, restored or merely archived", () => {
    expect(decide().historyNotice).toBe(ARCHIVE_HISTORY_NOTICE);
    expect(
      decide({
        request: request({ accepted: true }),
        projection: projection({ currentTaskId: "task-1", archived: [] }),
      }).historyNotice,
    ).toBe(ARCHIVE_HISTORY_NOTICE);
  });

  it("is absent when there are no archives", () => {
    expect(decide({ projection: projection({ archived: [] }) }).historyNotice).toBeNull();
    expect(decide({ projection: null }).historyNotice).toBeNull();
  });
});

describe("restoreRefusalCopy", () => {
  const NAMED = [
    "task_restore_unknown",
    "task_restore_not_adopted",
    "task_permission_denied",
    "task_applicability_unresolved",
    "task_capability_unsupported",
    "task_insufficient_evidence",
    "task_execution_limit",
  ];

  it("gives distinct, non-empty Chinese copy per named failure", () => {
    const copies = NAMED.map(restoreRefusalCopy);
    expect(new Set(copies).size).toBe(NAMED.length);
    for (const copy of copies) {
      expect(copy.length).toBeGreaterThan(0);
      expect(/[一-鿿]/.test(copy)).toBe(true);
    }
  });

  it("gives a non-empty generic line for an unmapped failure", () => {
    expect(restoreRefusalCopy("task_some_new_failure").length).toBeGreaterThan(0);
  });

  it("never turns an operational failure into a question", () => {
    const all = [...NAMED.map(restoreRefusalCopy), restoreRefusalCopy("unmapped")];
    for (const copy of all) {
      expect(copy).not.toContain("？");
      expect(copy).not.toContain("吗");
    }
  });
});

describe("the non-adoption copy never claims adoption", () => {
  it("keeps 已恢复 out of every state that did not prove it", () => {
    const copies = [
      RESTORE_AWAITING_COPY,
      RESTORE_UNRESOLVED_COPY,
      RESTORE_UNHEARD_COPY,
      restoreRefusalCopy("task_restore_not_adopted"),
      restoreRefusalCopy("task_restore_unknown"),
    ];
    for (const copy of copies) {
      expect(copy).not.toContain("已恢复");
      expect(copy).toContain("当前任务保持不变");
    }
    expect(RESTORE_ADOPTED_COPY).toContain("已恢复");
  });
});

describe("restorableArchiveIds", () => {
  it("drops blank ids and keeps the rest in host order", () => {
    expect(
      restorableArchiveIds([
        summary({ archive_id: "a" }),
        summary({ archive_id: "   " }),
        summary({ archive_id: "b" }),
      ]),
    ).toEqual(["a", "b"]);
  });

  it("offers nothing for an older host", () => {
    expect(restorableArchiveIds(undefined)).toEqual([]);
  });
});

describe("restoreIsListed (the send guard)", () => {
  it("allows only a concrete id this listing actually offers", () => {
    expect(restoreIsListed([summary({ archive_id: "a" })], "a")).toBe(true);
    expect(restoreIsListed([summary({ archive_id: "a" })], "b")).toBe(false);
    expect(restoreIsListed([summary({ archive_id: "a" })], "   ")).toBe(false);
    expect(restoreIsListed(undefined, "a")).toBe(false);
  });

  it("allows a blank id never to be sent, whatever the listing", () => {
    expect(restoreIsListed([summary({ archive_id: "" })], "")).toBe(false);
  });
});

describe("archiveOutcomeLabel", () => {
  it("names each outcome and marks an unrecorded one explicitly", () => {
    expect(archiveOutcomeLabel("active")).toBe("进行中");
    expect(archiveOutcomeLabel("complete")).toBe("已完成");
    expect(archiveOutcomeLabel(null)).toBe("未记录");
  });
});
