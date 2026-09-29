// What the visual-observation surface actually renders — and, just as
// importantly, what it never renders. The lane's whole purpose is to
// present a model-generated observation of a page WITH its provenance and
// a link back to the original location, while the quote channel (chips
// placed by ``answer.indexOf(quote)`` + the sources list) stays closed to
// it (spec thread-attachment-source-read lines 58 and 65).
//
// Rendered with ``renderToString`` because this project's vitest setup has
// no DOM testing library; string output is enough to pin the copy, the
// link's source_ref and the absence of a chip.

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";

import AnswerWithCitations from "./CitationChips";
import VisualClaims from "./VisualClaims";
import { parseVisualClaim } from "../visualClaims";
import type { Citation, Turn, VisualClaim } from "../types";

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

function visualClaim(overrides: Partial<VisualClaim> = {}): VisualClaim {
  return {
    kind: "visual_observation",
    attachment_id: "att-1",
    version: "3",
    unit: 4,
    page: 4,
    observation_version: 2,
    scope: [0.12, 0.05, 0.41, 0.62],
    provider: "configured-vision",
    model_version: "vision-model-1",
    page_image_sha256: "a".repeat(64),
    detail: "Revenue by segment chart / Services series / FY2024",
    filename: "quarterly-notes.pdf",
    reused: false,
    outbound_calls: 1,
    ...overrides,
  };
}

function attachmentCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "attachment",
    document_id: "",
    attachment_id: "att-1",
    version: "3",
    unit: 4,
    display: "page",
    filename: "quarterly-notes.pdf",
    quote: "渠道库存较上季度下降。",
    ...overrides,
  };
}

function render(claims: VisualClaim[]): string {
  return renderToString(
    createElement(VisualClaims, { threadId: THREAD_ID, claims }),
  );
}

function renderAnswer(answer: string, citations: Citation[]): string {
  return renderToString(createElement(AnswerWithCitations, { answer, citations }));
}

/** Visible text only — tags and React's ``<!-- -->`` separators gone, so a
 * sentence split across two text nodes still compares. */
function text(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ");
}

/** One attribute's value, HTML-unescaped so a JSON payload compares. */
function attr(html: string, name: string): string | undefined {
  const match = new RegExp(`${name}="([^"]*)"`).exec(html);
  if (!match) return undefined;
  return match[1]
    .replace(/&quot;/g, '"')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, "&");
}

/** The claim's own source link, so an attribute is read off the element it
 * belongs to and not off the section's ``aria-label``. */
function sourceLink(html: string): string {
  return /<a [^>]*data-testid="visual-claim-source"[^>]*>/.exec(html)?.[0] ?? "";
}

describe("one rendered visual observation", () => {
  it("names the attachment, the physical page and the target", () => {
    const visible = text(render([visualClaim({ page: 7 })]));
    expect(visible).toContain("quarterly-notes.pdf");
    expect(visible).toContain("p.7");
    expect(visible).toContain("Revenue by segment chart / Services series / FY2024");
  });

  it("says it is a generated visual observation, not a quote", () => {
    const visible = text(render([visualClaim()]));
    expect(visible).toContain("视觉观察");
    expect(visible).toContain("非原文引用");
    expect(visible).toContain("不是原文引用");
  });

  it("links to the original location with the attachment's source_ref", () => {
    const link = sourceLink(render([visualClaim()]));
    expect(attr(link, "href")).toBe(
      `/api/threads/${THREAD_ID}/attachments/att-1`,
    );
    expect(JSON.parse(attr(link, "data-source-ref") ?? "")).toEqual({
      kind: "attachment",
      attachment_id: "att-1",
      version: "3",
      unit: 4,
    });
  });

  it("labels the link so a screen reader hears a location, not a quote", () => {
    const link = sourceLink(render([visualClaim({ page: 7 })]));
    expect(attr(link, "aria-label")).toContain("quarterly-notes.pdf p.7");
  });

  it("keeps the raster hash that ties the claim to the page image", () => {
    expect(attr(render([visualClaim()]), "data-page-image-sha256")).toBe(
      "a".repeat(64),
    );
  });

  it("renders one row per claim", () => {
    const html = render([
      visualClaim(),
      visualClaim({ attachment_id: "att-2", filename: "chart.png" }),
    ]);
    expect(html.split('data-testid="visual-claim-row"').length - 1).toBe(2);
  });
});

describe("the region that was read", () => {
  it("shows the normalized bounds when a crop was inspected", () => {
    expect(text(render([visualClaim({ scope: [0.1, 0.2, 0.3, 0.4] })]))).toContain(
      "区域 0.10, 0.20, 0.30, 0.40",
    );
  });

  it("says whole page when no reliable box existed", () => {
    const visible = text(render([visualClaim({ scope: null })]));
    expect(visible).toContain("整页");
    expect(visible).not.toContain("区域 0.");
  });
});

describe("reused vs newly inspected", () => {
  it("marks a stored observation this run reused, with no call charged", () => {
    const visible = text(render([visualClaim({ reused: true, outbound_calls: 0 })]));
    expect(visible).toContain("复用已存观察");
    expect(visible).toContain("视觉调用 0 次");
  });

  it("marks a fresh inspection and its charged call", () => {
    const visible = text(render([visualClaim({ reused: false, outbound_calls: 3 })]));
    expect(visible).toContain("本次新读取页面");
    expect(visible).toContain("视觉调用 3 次");
    expect(visible).not.toContain("复用已存观察");
  });

  it("distinguishes two rows that differ only in reuse", () => {
    expect(render([visualClaim({ reused: true, outbound_calls: 0 })])).not.toBe(
      render([visualClaim({ reused: false, outbound_calls: 0 })]),
    );
  });

  it("stamps which observation version answered, from which model", () => {
    const visible = text(render([visualClaim({ observation_version: 5 })]));
    expect(visible).toContain("v5");
    expect(visible).toContain("configured-vision/vision-model-1");
  });
});

describe("an empty claim set renders nothing", () => {
  it("emits no markup at all", () => {
    expect(render([])).toBe("");
  });
});

describe("a visual claim never reaches the quote channel", () => {
  it("produces no chip, no mark and no quoted text", () => {
    const html = render([visualClaim({ detail: "营收同比增长 8%。" })]);
    expect(html).not.toContain("<mark");
    expect(html).not.toContain("“");
    expect(html).not.toContain("data-citation-index");
  });

  it("does not place its target into the answer text", () => {
    // The hazard: chips are positioned with ``answer.indexOf(c.quote)``.
    // A claim whose target happens to appear verbatim in the answer must
    // still produce no span there — only the real citation does.
    const answer = "营收同比增长 8%。渠道库存较上季度下降。";
    const chips = renderAnswer(answer, [attachmentCitation()]);
    expect(chips.split("<mark").length - 1).toBe(1);
    const claims = render([visualClaim({ detail: "营收同比增长 8%。" })]);
    expect(claims).not.toContain("<mark");
    expect(claims.split("营收同比增长 8%。").length - 1).toBe(1);
  });

  it("cannot produce a chip even when handed to the citation renderer", () => {
    // The structural guarantee stated at the renderer, not only at the
    // fold: the chip layer places a span with ``answer.indexOf(c.quote)``,
    // and a projected claim row carries no quote to place. Fed one anyway,
    // the renderer locates nothing — no mark, no index, no reveal.
    const projected = parseVisualClaim(
      visualClaim({ detail: "营收同比增长 8%。" }),
    );
    // Both id slots are filled so the fixture reaches the placement rule
    // itself rather than crashing on a missing identity: the point is that
    // a claim row has nothing quotable even when it names a real source.
    const asCitation = {
      ...projected,
      document_id: "",
      attachment_id: "",
    } as unknown as Citation;
    const chips = renderAnswer(
      "营收同比增长 8%。渠道库存较上季度下降。",
      [asCitation],
    );
    expect(chips).not.toContain("<mark");
    expect(chips).not.toContain("data-citation-index");
    expect(text(chips)).toContain("“”");
  });

  it("leaves the sources list driven by citations alone", () => {
    // One attachment citation and one claim naming the SAME source and
    // unit: the sources list still reports that one span, and the two
    // surfaces stay separate — the claim neither joins the list nor
    // pulls the citation into its own row.
    const citation = attachmentCitation();
    const claim = visualClaim();
    const turn: Turn = {
      run_id: "22222222-2222-4222-8222-222222222222",
      question: "渠道库存怎么样？",
      status: "done",
      answer: "渠道库存较上季度下降。",
      citations: [citation],
      visual_claims: [claim],
      created_at: "2026-01-01T00:00:00.000Z",
    };
    const chips = renderAnswer(turn.answer, turn.citations);
    const claims = render(turn.visual_claims ?? []);
    expect(text(chips)).toContain("1 span");
    expect(chips).not.toContain("视觉观察");
    expect(chips).not.toContain(claim.detail);
    expect(claims).not.toContain(citation.quote);
    expect(claims).not.toContain("Sources");
  });
});
