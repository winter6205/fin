// The task-door refusal vocabulary is a CROSS-LANGUAGE contract: the
// Python host decides refusals with ``fin.host.task.task_runtime_types.
// TASK_FAILURE_KINDS`` and the SPA branches on ``TASK_FAILURE_KINDS``
// in web/src/types.ts. The two sets are mirrored BY HAND, so the real
// risk is drift: a token added on one side only makes the other side
// misread a named refusal (a task-door 400/409 or a terminal ``refuse``
// frame with ``status_classified``) as an ordinary run failure.
//
// This test pins the TS set to a committed golden fixture generated
// from the Python source of truth. It is a full-set assertion — same
// members AND same size — so a token added on one side alone fails
// here. Regenerate the fixture from the Python symbol when the
// vocabulary legitimately changes; the command is recorded in the
// fixture itself.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { TASK_FAILURE_KINDS, isTaskFailureKind } from "./types";

const GOLDEN = join(
  dirname(fileURLToPath(import.meta.url)),
  "taskFailureKinds.golden.json",
);

interface TaskFailureKindsGolden {
  source: string;
  generated_by: string;
  kinds: string[];
}

function golden(): TaskFailureKindsGolden {
  return JSON.parse(readFileSync(GOLDEN, "utf8")) as TaskFailureKindsGolden;
}

describe("TASK_FAILURE_KINDS mirrors the Python vocabulary", () => {
  it("equals the committed golden fixture exactly (same members, same size)", () => {
    const expected = golden().kinds;
    expect(TASK_FAILURE_KINDS.size).toBe(expected.length);
    expect([...TASK_FAILURE_KINDS].sort()).toEqual([...expected].sort());
  });

  it("recognises every golden member as a task failure kind", () => {
    for (const kind of golden().kinds) {
      expect(isTaskFailureKind(kind), `isTaskFailureKind(${kind})`).toBe(true);
    }
  });

  it("rejects tokens outside the vocabulary", () => {
    // ``step_cap`` is a REAL non-task refusal reason on the same wire
    // (specs' run refusal lanes) — the exact token a drift would
    // misread as a task-door refusal.
    expect(isTaskFailureKind("step_cap")).toBe(false);
    expect(isTaskFailureKind("no-such-task-failure-kind")).toBe(false);
    // The guard's own contract: not-a-string is simply not a member.
    expect(isTaskFailureKind(null)).toBe(false);
    expect(isTaskFailureKind(undefined)).toBe(false);
  });

  it("names the Python symbol the fixture was generated from", () => {
    expect(golden().source).toBe(
      "fin.host.task.task_runtime_types.TASK_FAILURE_KINDS",
    );
  });
});
