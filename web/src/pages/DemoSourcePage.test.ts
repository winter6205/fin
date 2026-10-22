import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { DemoSourceViewer } from "./DemoSourcePage";
import type { DemoSource } from "../demoMode";

const source: DemoSource = {
  document_id: "zeus-handbook",
  version: "a".repeat(64),
  unit: 1,
  page: 1,
  title: "ZEUS Employee Handbook",
  section: "Section 1",
  content: "ZEUS Employee Handbook · Page 1\nSection 1\nAnnual leave accrues at 22 days.",
};

describe("DemoSourceViewer", () => {
  it("shows the matched document, section, page, and full source chunk", () => {
    const html = renderToString(createElement(DemoSourceViewer, { source }));

    expect(html).toContain('data-testid="demo-source-viewer"');
    expect(html).toContain('data-testid="demo-source-title"');
    expect(html).toContain("ZEUS Employee Handbook");
    expect(html).toContain('data-testid="demo-source-section"');
    expect(html).toContain("Section 1");
    expect(html).toContain('data-testid="demo-source-page"');
    expect(html).toContain("Page 1");
    expect(html).toContain('data-testid="demo-source-content"');
    expect(html).toContain("Annual leave accrues at 22 days.");
  });
});
