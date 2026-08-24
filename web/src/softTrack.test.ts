import { describe, expect, it } from "vitest";
import {
  parseSoftTrack,
  parseTrackDecision,
  resolveTurnTrack,
  showResearchTrackHint,
} from "./softTrack";

describe("showResearchTrackHint", () => {
  it("shows the light hint only when track is research", () => {
    expect(showResearchTrackHint("research")).toBe(true);
  });

  it("hides the hint for dialogue and other non-research tracks", () => {
    expect(showResearchTrackHint("dialogue")).toBe(false);
    expect(showResearchTrackHint("clarify")).toBe(false);
    expect(showResearchTrackHint("off_topic")).toBe(false);
  });

  it("hides the hint when track is missing", () => {
    expect(showResearchTrackHint(undefined)).toBe(false);
    expect(showResearchTrackHint(null)).toBe(false);
  });
});

describe("parseSoftTrack", () => {
  it("accepts known Soft-Track enums and rejects unknowns", () => {
    expect(parseSoftTrack("research")).toBe("research");
    expect(parseSoftTrack("dialogue")).toBe("dialogue");
    expect(parseSoftTrack("chat")).toBeUndefined();
    expect(parseSoftTrack(null)).toBeUndefined();
  });
});

describe("resolveTurnTrack", () => {
  it("prefers top-level track over track_decision", () => {
    expect(
      resolveTurnTrack({
        track: "research",
        track_decision: { track: "dialogue" },
      }),
    ).toBe("research");
  });

  it("falls back to track_decision.track", () => {
    expect(resolveTurnTrack({ track_decision: { track: "research" } })).toBe(
      "research",
    );
  });
});

describe("parseTrackDecision", () => {
  it("returns undefined for malformed payloads", () => {
    expect(parseTrackDecision(null)).toBeUndefined();
    expect(parseTrackDecision({ track: "nope" })).toBeUndefined();
  });
});
