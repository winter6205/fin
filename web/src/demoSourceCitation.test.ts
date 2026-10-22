// Persisted demo citations resolve only through the exact fixture index.

import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";
import { describe, expect, it } from "vitest";

import DemoModeSurface from "./components/DemoModeSurface";
import AnswerWithCitations from "./capabilities/evidence/components/CitationChips";
import { PRODUCT_ROUTE_PATHS } from "./productRoutes";
import type { Citation } from "./types";
import type { DemoSourceSummary } from "./demoMode";

const CITATION: Citation = {
  kind: "company",
  document_id: "zeus-handbook",
  attachment_id: "",
  version: "a".repeat(64),
  unit: 1,
  display: "page",
  filename: "",
  quote: "Annual leave accrues at 22 days per calendar year, credited to employees.",
};

const SOURCE: DemoSourceSummary = {
  document_id: "zeus-handbook",
  version: "a".repeat(64),
  unit: 1,
  page: 1,
  title: "ZEUS Employee Handbook",
  section: "Section 1",
};

function render(citation: Citation, sourceIndex: DemoSourceSummary[]): string {
  return renderToString(
    createElement(
      StaticRouter,
      { location: "/t/thread-1" },
      createElement(
        DemoModeSurface,
        { enabled: true, sourceIndex },
        createElement(
          Routes,
          null,
          createElement(Route, {
            path: PRODUCT_ROUTE_PATHS[1],
            element: createElement(AnswerWithCitations, {
              answer: citation.quote,
              citations: [citation],
            }),
          }),
        ),
      ),
    ),
  );
}

describe("demo fixture source links", () => {
  it("links a matching persisted citation and supplies its trusted section", () => {
    const html = render(CITATION, [SOURCE]);
    const link = /<a[^>]*data-testid="citation-source-link"[^>]*>[\s\S]*?<\/a>/.exec(html)?.[0] ?? "";

    expect(link).toContain("/demo/source?");
    expect(link).toContain("document_id=zeus-handbook");
    expect(link).toContain(`version=${SOURCE.version}`);
    expect(link).toContain("unit=1");
    expect(link).toContain("page=1");
    expect(link).toContain("ZEUS Employee Handbook");
    expect(link).toContain("Section 1 · p.1");
  });

  it("keeps a stale citation as plain text and does not invent a viewer target", () => {
    const html = render(CITATION, [{ ...SOURCE, version: "b".repeat(64) }]);

    expect(html).not.toContain('data-testid="citation-source-link"');
    expect(html).not.toContain('href="/demo/source?');
    expect(html).toContain("章节未注明 · p.1");
  });
});
