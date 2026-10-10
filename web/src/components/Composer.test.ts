// The draft's survival rule across one send.
//
// The textarea holds the operator's only copy of the question, so the seam
// between "sent" and "typed but never sent" is what decides whether their
// input still exists after the composer returns. specs/question-workspace.md
// §"Active work, outcomes, and history" requires that nothing the workspace
// does silently destroys input: the box is emptied as the question is handed
// over to the transport, and a send that never started puts it back.
//
// Pure seam: the shipped submit handler calls ``draftAfterSend`` with the
// store's own answer, so the rule is asserted here rather than through a DOM
// (this project's vitest setup has no DOM testing library).

import { describe, expect, it } from "vitest";
import { draftAfterSend } from "./Composer";

const QUESTION = "要比较哪两个期间？";

describe("a send the transport took", () => {
  it("leaves the box exactly as the handover put it", () => {
    // ``submit`` empties the box when it hands the question over, so a
    // taken send has nothing left to consume.
    expect(draftAfterSend("", QUESTION, true)).toBe("");
  });

  it("keeps text the operator typed while the request was in flight", () => {
    // Input stays available during a run, so a late-entered follow-up is
    // not the question that was sent and cannot be discarded by it.
    const typed = `${QUESTION}\n还要按季度拆分`;
    expect(draftAfterSend(typed, QUESTION, true)).toBe(typed);
  });

  it("cannot discard the question a second time", () => {
    // Re-typing the very same question during the run is new input, not
    // residue: nothing on this path may empty the box.
    expect(draftAfterSend(QUESTION, QUESTION, true)).toBe(QUESTION);
  });
});

describe("a send that never started", () => {
  it("puts the question back in the emptied box", () => {
    // The store refused the send before the transport saw it, so the
    // operator's text was taken by no one and must be recoverable.
    expect(draftAfterSend("", QUESTION, false)).toBe(QUESTION);
  });

  it("keeps anything typed since the send was refused", () => {
    // A refusal cannot overwrite input the operator moved on from.
    for (const current of [`${QUESTION}\n补充说明`, "  ", QUESTION]) {
      expect(draftAfterSend(current, QUESTION, false)).toBe(current);
    }
  });
});
