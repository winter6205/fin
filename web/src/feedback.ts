/** Client helper for the T8 thumbs feedback endpoint.
 *
 * The workspace route will be wired in a later ticket. Keeping this boundary
 * separate lets the UI adopt the exact payload without changing the shared
 * store or turn types wholesale.
 */

export type FeedbackValue = "up" | "down";

export interface FeedbackRecord {
  run_id: string;
  value: FeedbackValue;
  created_at: string;
  updated_at: string;
}

export class InvalidFeedbackError extends Error {}

function validateInput(
  threadId: string,
  runId: string,
  value: string,
): asserts value is FeedbackValue {
  if (!threadId.trim() || !runId.trim()) {
    throw new InvalidFeedbackError("thread and run ids are required");
  }
  if (value !== "up" && value !== "down") {
    throw new InvalidFeedbackError("feedback value must be 'up' or 'down'");
  }
}

function parseFeedback(value: unknown): FeedbackRecord {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("malformed feedback payload");
  }
  const row = value as Record<string, unknown>;
  if (
    typeof row.run_id !== "string" ||
    typeof row.value !== "string" ||
    (row.value !== "up" && row.value !== "down") ||
    typeof row.created_at !== "string" ||
    typeof row.updated_at !== "string"
  ) {
    throw new Error("malformed feedback payload");
  }
  return {
    run_id: row.run_id,
    value: row.value,
    created_at: row.created_at,
    updated_at: row.updated_at,
  };
}

export async function submitFeedback(
  threadId: string,
  runId: string,
  value: string,
): Promise<FeedbackRecord> {
  validateInput(threadId, runId, value);
  const response = await fetch(
    `/api/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/feedback`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ value }),
    },
  );
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      if (typeof body === "object" && body !== null && "error" in body) {
        detail = String((body as Record<string, unknown>).error);
      }
    } catch {
      // The status text is the only safe fallback for a non-JSON error.
    }
    throw new Error(`HTTP ${response.status}: ${detail}`);
  }
  return parseFeedback(await response.json());
}
