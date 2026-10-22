import { describe, expect, it, vi } from "vitest";

import {
  DemoApiError,
  getDemoSource,
  getDemoSourceIndex,
  getDemoStatus,
  resetDemoData,
} from "./demoMode";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("demo mode HTTP client", () => {
  it("reads only the server's explicit enabled flag", async () => {
    const fetcher = vi.fn(async () => response({ enabled: true }));

    await expect(getDemoStatus(fetcher)).resolves.toEqual({ enabled: true });
    expect(fetcher).toHaveBeenCalledWith("/api/demo");
  });

  it("rejects an invalid demo status payload with a typed error", async () => {
    const fetcher = vi.fn(async () => response({ enabled: "yes" }));

    await expect(getDemoStatus(fetcher)).rejects.toMatchObject({
      name: "DemoApiError",
      code: "malformed_demo_status",
      status: 200,
    });
  });

  it("uses a POST with no caller-provided reset command", async () => {
    const fetcher = vi.fn(async () => response({ status: "reset" }));

    await expect(resetDemoData(fetcher)).resolves.toBeUndefined();
    expect(fetcher).toHaveBeenCalledWith("/api/demo/reset", {
      method: "POST",
    });
  });

  it("surfaces the server's stable reset error code", async () => {
    const fetcher = vi.fn(async () =>
      response({ error: "demo_configuration_missing" }, 503),
    );

    await expect(resetDemoData(fetcher)).rejects.toMatchObject({
      name: "DemoApiError",
      code: "demo_configuration_missing",
      status: 503,
    });
  });

  it("converts an unreachable host into a typed status error", async () => {
    const fetcher = vi.fn(async () => {
      throw new TypeError("connection detail");
    });

    const result = getDemoStatus(fetcher);
    await expect(result).rejects.toBeInstanceOf(DemoApiError);
    await expect(result).rejects.toMatchObject({
      code: "demo_host_unavailable",
    });
  });

  it("loads a compact source index and rejects malformed source rows", async () => {
    const source = {
      document_id: "zeus-handbook",
      version: "a".repeat(64),
      unit: 1,
      page: 1,
      title: "ZEUS Employee Handbook",
      section: "Section 1",
    };
    const fetcher = vi.fn(async () => response({ sources: [source] }));

    await expect(getDemoSourceIndex(fetcher)).resolves.toEqual([source]);
    expect(fetcher).toHaveBeenCalledWith("/api/demo/sources");
    await expect(
      getDemoSourceIndex(async () => response({ sources: [{ ...source, unit: 0 }] })),
    ).rejects.toMatchObject({ code: "malformed_demo_sources" });
  });

  it("requests one exact demo locator including its page and renders no command input", async () => {
    const source = {
      document_id: "zeus handbook/2024",
      version: "a".repeat(64),
      unit: 2,
      page: 2,
      title: "ZEUS Employee Handbook",
      section: "Section 2",
      content: "ZEUS Employee Handbook · Page 2\nSection 2 covers leave.",
    };
    const fetcher = vi.fn(async (_url: RequestInfo | URL) => response(source));

    await expect(getDemoSource(source, fetcher)).resolves.toEqual(source);
    const requestedUrl = String(fetcher.mock.calls[0]?.[0]);
    expect(requestedUrl).toContain("/api/demo/source?");
    expect(requestedUrl).toContain("document_id=zeus+handbook%2F2024");
    expect(requestedUrl).toContain(`version=${source.version}`);
    expect(requestedUrl).toContain("unit=2");
    expect(requestedUrl).toContain("page=2");
  });
});
