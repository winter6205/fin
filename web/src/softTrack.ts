// Soft-Track UI helpers. Wire fields are optional until the host
// peer lands SSE/GET ``track`` / ``track_decision``; missing track
// must not break history or streaming.

import type { SoftTrack, TrackDecision, Turn } from "./types";

const SOFT_TRACKS: ReadonlySet<string> = new Set([
  "dialogue",
  "clarify",
  "off_topic",
  "grounded",
]);

const TRACK_ALIASES: Readonly<Record<string, SoftTrack>> = {
  research: "grounded",
};

/** Defensive parse: unknown / missing values become ``undefined``. */
export function parseSoftTrack(raw: unknown): SoftTrack | undefined {
  if (typeof raw !== "string") return undefined;
  const mapped = TRACK_ALIASES[raw] ?? raw;
  return SOFT_TRACKS.has(mapped) ? (mapped as SoftTrack) : undefined;
}

/** Defensive parse of the optional RunRecord audit blob. */
export function parseTrackDecision(raw: unknown): TrackDecision | undefined {
  if (!raw || typeof raw !== "object") return undefined;
  const obj = raw as Record<string, unknown>;
  const track = parseSoftTrack(obj.track);
  if (track === undefined) return undefined;
  return {
    track,
    confidence: typeof obj.confidence === "number" ? obj.confidence : undefined,
    decision_tier:
      typeof obj.decision_tier === "string" ? obj.decision_tier : undefined,
    signals:
      obj.signals && typeof obj.signals === "object" && obj.signals !== null
        ? (obj.signals as Record<string, unknown>)
        : undefined,
  };
}

/** Prefer top-level ``track``, then ``track_decision.track``. */
export function resolveTurnTrack(
  turn: Pick<Turn, "track" | "track_decision">,
): SoftTrack | undefined {
  return turn.track ?? turn.track_decision?.track;
}

/** Light grounded-leaf hint — never for dialogue / clarify / off_topic. */
export function showResearchTrackHint(
  track: SoftTrack | undefined | null,
): boolean {
  return track === "grounded";
}
