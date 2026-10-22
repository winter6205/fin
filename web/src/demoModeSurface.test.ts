import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";

import DemoModeSurface from "./components/DemoModeSurface";

const SRC_DIR = dirname(fileURLToPath(import.meta.url));

describe("DemoModeSurface", () => {
  it("keeps one demo badge visible around a thread route", () => {
    const html = renderToString(
      createElement(StaticRouter, { location: "/t/thread-1" },
        createElement(DemoModeSurface, { enabled: true },
          createElement(Routes, null,
            createElement(Route, {
              path: "/t/:thread_id",
              element: createElement(
                "main",
                { "data-testid": "thread-page" },
                "对话内容",
              ),
            }),
          ),
        ),
      ),
    );

    expect(html).toContain('data-testid="demo-mode-badge"');
    expect(html).toContain('data-testid="thread-page"');
    expect(html.match(/data-testid="demo-mode-badge"/g)).toHaveLength(1);
  });

  it("wraps App routes and leaves the badge out of the Home panel", () => {
    const app = readFileSync(join(SRC_DIR, "App.tsx"), "utf8");
    const home = readFileSync(join(SRC_DIR, "pages", "HomePage.tsx"), "utf8");

    expect(app).toMatch(
      /<DemoModeSurface enabled=\{demoModeEnabled\}>[\s\S]*?<Routes>[\s\S]*?<\/Routes>[\s\S]*?<\/DemoModeSurface>/,
    );
    expect(home).not.toContain('data-testid="demo-mode-badge"');
  });

  it("does not render a badge outside demo mode", () => {
    const html = renderToString(
      createElement(
        DemoModeSurface,
        { enabled: false },
        createElement("main", { "data-testid": "thread-page" }, "对话内容"),
      ),
    );

    expect(html).not.toContain('data-testid="demo-mode-badge"');
    expect(html).toContain('data-testid="thread-page"');
  });
});
