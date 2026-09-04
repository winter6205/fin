// T3 (ADR 0044): the thread-detail occupancy indicator.
//
// The project's vitest setup runs without a DOM testing library,
// so — following the ``statusStrip.ts`` convention — the wording
// and the neutral-state decision are pinned here as pure helpers,
// and the ThreadPage render is a 1-line use of them.

import { describe, expect, it } from "vitest";

import type { ThreadDetail } from "./types";
import { occupancyPercent, parseThreadOccupancy } from "./threadOccupancy";

function makeDetail(overrides: Record<string, unknown> = {}): ThreadDetail {
  return {
    id: "11111111-1111-4111-8111-111111111111",
    title: "AAPL 10-Q",
    created_at: "2026-01-01T00:00:00.000Z",
    turns: [],
    ...overrides,
  } as ThreadDetail;
}

describe("parseThreadOccupancy", () => {
  it("accepts the host occupancy object", () => {
    const detail = makeDetail({
      occupancy: {
        estimated_tokens: 1024,
        budget_tokens: 256000,
        ratio: 0.004,
      },
    });
    expect(parseThreadOccupancy(detail)).toEqual({
      estimated_tokens: 1024,
      budget_tokens: 256000,
      ratio: 0.004,
    });
  });

  it("returns null for a never-run thread (no occupancy field)", () => {
    expect(parseThreadOccupancy(makeDetail())).toBeNull();
  });

  it("returns null for an explicit null occupancy", () => {
    expect(parseThreadOccupancy(makeDetail({ occupancy: null }))).toBeNull();
  });

  it("rejects a malformed occupancy rather than rendering a made-up figure", () => {
    expect(
      parseThreadOccupancy(makeDetail({ occupancy: { ratio: 0.5 } })),
    ).toBeNull();
    expect(
      parseThreadOccupancy(
        makeDetail({
          occupancy: { estimated_tokens: "many", budget_tokens: 1, ratio: 1 },
        }),
      ),
    ).toBeNull();
    expect(
      parseThreadOccupancy(
        makeDetail({
          occupancy: { estimated_tokens: 1, budget_tokens: 0, ratio: 1 },
        }),
      ),
    ).toBeNull();
    expect(
      parseThreadOccupancy(
        makeDetail({
          occupancy: { estimated_tokens: 1, budget_tokens: 100, ratio: -1 },
        }),
      ),
    ).toBeNull();
  });
});

describe("occupancyPercent", () => {
  it("renders the floored percentage", () => {
    expect(
      occupancyPercent({
        estimated_tokens: 3072,
        budget_tokens: 256000,
        ratio: 0.012,
      }),
    ).toBe("1%");
    expect(
      occupancyPercent({
        estimated_tokens: 128000,
        budget_tokens: 256000,
        ratio: 0.5,
      }),
    ).toBe("50%");
  });

  it("floors 0.909 to 90% (never rounds up past the trigger)", () => {
    expect(
      occupancyPercent({
        estimated_tokens: 0,
        budget_tokens: 256000,
        ratio: 0.909,
      }),
    ).toBe("90%");
  });

  it("renders the clamped 100% only when the host says 1.0", () => {
    expect(
      occupancyPercent({
        estimated_tokens: 300000,
        budget_tokens: 256000,
        ratio: 1,
      }),
    ).toBe("100%");
  });
});
