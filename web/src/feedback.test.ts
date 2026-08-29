import { afterEach, describe, expect, it, vi } from "vitest";
import { submitFeedback } from "./feedback";

afterEach(() => {
  vi.restoreAllMocks();
});

describe("submitFeedback", () => {
  it("posts the selected thumb and parses the persisted feedback", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(
        new Response(
          JSON.stringify({
            run_id: "run-1",
            value: "up",
            created_at: "2026-08-29T00:00:00Z",
            updated_at: "2026-08-29T00:00:00Z",
          }),
          { status: 200, headers: { "Content-Type": "application/json" } },
        ),
      );

    await expect(submitFeedback("thread-1", "run-1", "up")).resolves.toEqual({
      run_id: "run-1",
      value: "up",
      created_at: "2026-08-29T00:00:00Z",
      updated_at: "2026-08-29T00:00:00Z",
    });

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/threads/thread-1/runs/run-1/feedback",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ value: "up" }),
      }),
    );
  });

  it("rejects invalid feedback before making a request", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch");

    await expect(submitFeedback("thread-1", "run-1", "sideways")).rejects.toThrow(
      "feedback value",
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
