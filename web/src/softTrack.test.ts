import { describe, expect, it } from "vitest";
import {
  parseSoftTrack,
  parseTrackDecision,
  resolveTurnTrack,
  showResearchTrackHint,
} from "./softTrack";

describe("showResearchTrackHint", () => {
  it("shows the light hint only when track is grounded", () => {
    expect(showResearchTrackHint("grounded")).toBe(true);
  });

  it("hides the hint for dialogue and other non-grounded tracks", () => {
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
    expect(parseSoftTrack("grounded")).toBe("grounded");
    expect(parseSoftTrack("research")).toBe("grounded");
    expect(parseSoftTrack("dialogue")).toBe("dialogue");
    expect(parseSoftTrack("chat")).toBeUndefined();
    expect(parseSoftTrack(null)).toBeUndefined();
  });
});

describe("resolveTurnTrack", () => {
  it("prefers top-level track over track_decision", () => {
    expect(
      resolveTurnTrack({
        track: "grounded",
        track_decision: { track: "dialogue" },
      }),
    ).toBe("grounded");
  });

  it("falls back to track_decision.track", () => {
    expect(resolveTurnTrack({ track_decision: { track: "grounded" } })).toBe(
      "grounded",
    );
  });
});

describe("parseTrackDecision", () => {
  it("returns undefined for malformed payloads", () => {
    expect(parseTrackDecision(null)).toBeUndefined();
    expect(parseTrackDecision({ track: "nope" })).toBeUndefined();
  });
});
