// What the citation surface actually renders for the evidence-refusal
// lane and the database-row snapshot identity. The decoder guarantees a
// refusal is a TYPED visible outcome; this file pins the other half of
// that promise — that the visible named-code indicator and the stored
// snapshot identity really reach the rendered HTML, and that a refused
// row never degrades into an empty success.
//
// Rendered with ``renderToString`` (same precedent as the visual-claims
// surface tests): this project's vitest setup has no DOM testing
// library, and string output is enough to pin copy and attributes.

import { describe, expect, it } from "vitest";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { Route, Routes } from "react-router-dom";
import { StaticRouter } from "react-router-dom/server";

import AnswerWithCitations from "./CitationChips";
import { PRODUCT_ROUTE_PATHS } from "../../../productRoutes";
import { threadAttachmentUrl } from "../../../sourceLocator";
import { databaseRowFieldLines, forbiddenDefaultLabels } from "../../../evidenceContract";
import type { Citation, DatabaseScalar, EvidenceRefusal } from "../../../types";

const QUOTE = "Our quarterly revenue rose 8% year over year.";

function companyCitation(overrides: Partial<Citation> = {}): Citation {
  return {
    kind: "company",
    document_id: "AAPL-10Q-2024Q3",
    attachment_id: "",
    version: "v1",
    unit: 3,
    display: "page",
    filename: "",
    quote: QUOTE,
    ...overrides,
  };
}

function databaseRowCitation(
  fields: Record<string, DatabaseScalar> = {
    counterparty: "Northwind Logistics",
    notice_days: "30",
  },
): Citation {
  return {
    kind: "database-row",
    document_id: "",
    attachment_id: "",
    version: "2026-03-01T09:14:22Z",
    unit: 1,
    display: "record",
    filename: "",
    quote: "C-2291 Northwind Logistics, notice period 30 days",
    database_row: {
      source: "ledger_ro.contract_expiry",
      record_key: { contract_id: "C-2291" },
      queried_at: "2026-03-01T09:14:22Z",
      fields,
      summary: "C-2291 Northwind Logistics, notice period 30 days",
    },
  };
}

/** An attachment-backed citation: the kind whose source lives behind the
 * host's attachment door, and the only kind that can jump there. */
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

/** The DEFAULT (non-developer) rendering — the view a reader gets. */
function render(answer: string, citations: Citation[], refusals?: EvidenceRefusal[]): string {
  return renderAs(answer, citations, refusals, false);
}

/** ``developerMode`` is injected rather than read from storage, so each
 * test states which of the two views it is pinning. */
function renderAs(
  answer: string,
  citations: Citation[],
  refusals: EvidenceRefusal[] | undefined,
  developerMode: boolean,
): string {
  return renderToString(
    createElement(AnswerWithCitations, {
      answer,
      citations,
      refusals,
      developerMode,
    }),
  );
}

/** Visible text only — tags and React's ``<!-- -->`` separators gone. */
function text(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ");
}

function refusalNodes(html: string): string[] {
  return html.match(/<li[^>]*data-testid="evidence-refusal"[^>]*>[\s\S]*?<\/li>/g) ?? [];
}

describe("the refusal indicator", () => {
  it("renders one visible row per refusal, in Chinese, with the code for developer mode", () => {
    // PREMISE CHANGED (the default view). This asserted the §6 code
    // string as VISIBLE copy, which is the internal accounting T10
    // removes: a reader cannot act on ``malformed_evidence_contract``.
    // The code now stays reachable in developer mode, and in the markup
    // as ``data-code`` in both views.
    const refusals: EvidenceRefusal[] = [
      { citation_index: 1, error: "malformed_evidence_contract" },
      { citation_index: 4, error: "unsupported_evidence_contract" },
    ];
    const nodes = refusalNodes(render(QUOTE, [], refusals));
    expect(nodes).toHaveLength(2);
    expect(nodes[0]).toContain('data-code="malformed_evidence_contract"');
    expect(text(nodes[0])).not.toContain("malformed_evidence_contract");
    expect(text(nodes[0])).toContain("第 2 条出处");
    expect(nodes[1]).toContain('data-code="unsupported_evidence_contract"');
    expect(text(nodes[1])).toContain("第 5 条出处");

    const devNodes = refusalNodes(renderAs(QUOTE, [], refusals, true));
    expect(text(devNodes[0])).toContain("malformed_evidence_contract");
    expect(text(devNodes[1])).toContain("unsupported_evidence_contract");
  });

  it("stays visible when every row was refused — never an empty success", () => {
    // The display policy's core case: an empty citation list alongside a
    // refusal must not read as "no sources needed". The refusal line is
    // the only evidence surface left, so it has to be present.
    const html = render("营收同比增长。", [], [
      { citation_index: 0, error: "citation_overflow" },
    ]);
    expect(html).not.toContain('aria-label="Sources"');
    expect(refusalNodes(html)).toHaveLength(1);
    expect(refusalNodes(html)[0]).toContain('data-code="citation_overflow"');
  });

  it("keeps surviving citations rendering normally next to a refusal", () => {
    const answer = `${QUOTE} 其它内容。`;
    const html = render(answer, [companyCitation()], [
      { citation_index: 1, error: "unknown_locator" },
    ]);
    // The valid row keeps its chip and its sources entry…
    expect(html).toContain("data-citation-index");
    expect(html).toContain('aria-label="Sources"');
    // …and the refused row is named beside them, not merged into the list.
    expect(refusalNodes(html)).toHaveLength(1);
    expect(refusalNodes(html)[0]).toContain('data-code="unknown_locator"');
  });

  it("emits no indicator markup for an empty or absent refusal list", () => {
    expect(render(QUOTE, [companyCitation()], [])).not.toContain("evidence-refusal");
    expect(render(QUOTE, [companyCitation()])).not.toContain("evidence-refusal");
  });
});

describe("the database-row snapshot identity", () => {
  const answer = "合同 C-2291 到期于 2026-03-14。";

  it("reads as ordinary Chinese by default and keeps the raw identity for developer mode", () => {
    // PREMISE CHANGED: this asserted ``来源 ledger_ro.contract_expiry ·
    // 记录 contract_id=C-2291`` as default copy — a SQL view name and a
    // snake_case record key. The raw identity is developer-mode copy.
    const identityOf = (html: string): string => {
      const node =
        /<p[^>]*data-testid="database-row-identity"[^>]*>[\s\S]*?<\/p>/.exec(html);
      expect(node).not.toBeNull();
      return text(node?.[0] ?? "");
    };
    const defaultText = identityOf(render(answer, [databaseRowCitation()]));
    expect(defaultText.trim()).toBe("一条记录 · 数据截至 2026年03月01日");
    expect(defaultText).not.toContain("ledger_ro.contract_expiry");

    const devText = identityOf(
      renderAs(answer, [databaseRowCitation()], undefined, true),
    );
    expect(devText).toContain("来源 ledger_ro.contract_expiry");
    expect(devText).toContain("contract_id=C-2291");
  });

  it("shows when the data was true and claims no currency", () => {
    // PREMISE CHANGED: this read the English ``as of <ts> — a snapshot
    // of that query, not a live read``. The Chinese keeps the same honest
    // claim — these are the values as of a moment, not a live read — and
    // claims nothing beyond it.
    const visible = text(render(answer, [databaseRowCitation()]));
    expect(visible).toContain("数据截至 2026年03月01日");
    expect(visible).toContain("不是实时读取");
    expect(visible).not.toContain("still valid");
    expect(
      text(renderAs(answer, [databaseRowCitation()], undefined, true)),
    ).toContain("not a live read");
  });

  it("renders scalar record_key values to text for developer mode, in name order", () => {
    // The snapshot's scalar widening: numbers/booleans/null reach the
    // identity line as their canonical text form, keys sorted by name —
    // the same rule the digest uses, kept in one place. The DEFAULT view
    // no longer prints the record key at all; this case pins the raw one.
    const citation = databaseRowCitation();
    citation.database_row = {
      ...citation.database_row!,
      record_key: { notice_days: 30, frozen: true, memo: null },
    };
    const identity = /<p[^>]*data-testid="database-row-identity"[^>]*>[\s\S]*?<\/p>/.exec(
      renderAs(answer, [citation], undefined, true),
    );
    expect(text(identity?.[0] ?? "")).toContain(
      "记录 frozen=true memo= notice_days=30",
    );
  });

  it("keeps the retained VALUES visible by default, dropping only the column names", () => {
    // A reader who wants to check C-2291 against the answer must still
    // find it; only the schema column names are developer-mode detail.
    // The field list lives behind the row's own 引文 control, so this
    // checks the lines the row hands it — the render path is pinned by
    // ``databaseRowFieldLines`` in evidenceContract.test.ts.
    const lines = databaseRowFieldLines(
      databaseRowCitation().database_row!,
      false,
    );
    expect(lines).toContain("Northwind Logistics");
    expect(lines.join("\n")).not.toContain("counterparty");
  });

  it("keeps document citations free of the database identity line", () => {
    expect(render(QUOTE, [companyCitation()])).not.toContain("database-row-identity");
  });
});

// specs/demo-seed.md item 6 / ADR 0100 decision 6: "Citations are inline
// superscripts placed after the sentence they support". The marker is a
// superscript `[N]` sitting IMMEDIATELY AFTER the span it supports — not a
// block, and not a hover-only decoration.
describe("inline superscript citations", () => {
  const QUOTE_A = "Our quarterly revenue rose 8% year over year.";
  const QUOTE_B = "Gross margin reached 45%.";
  // Two sentences, each carrying one verified span, so the superscripts
  // have two distinct positions to be ordered against.
  const PROSE = `${QUOTE_A} 营收同比增长 8%。${QUOTE_B} 毛利率为 45%。`;

  function twoCitations(): Citation[] {
    return [
      companyCitation({ quote: QUOTE_A }),
      companyCitation({
        quote: QUOTE_B,
        document_id: "AAPL-10Q-2024Q2",
        unit: 7,
      }),
    ];
  }

  /** The visible text of every rendered superscript marker, in order. */
  function superscripts(html: string): string[] {
    return (html.match(/<sup[^>]*>([\s\S]*?)<\/sup>/g) ?? []).map((node) =>
      node.replace(/<[^>]*>/g, ""),
    );
  }

  it("renders one 1-based [N] superscript per cited span, in prose order", () => {
    const html = render(PROSE, twoCitations());
    expect(superscripts(html)).toEqual(["[1]", "[2]"]);
  });

  it("places each superscript after the cited sentence", () => {
    const html = render(PROSE, twoCitations());
    // Each quote contains its sentence-ending punctuation, so these
    // sentence-spanning citations place the marker directly after it.
    expect(html).toMatch(
      /<mark[^>]*data-citation-index="0"[^>]*>[\s\S]*?<\/mark><\/span><button[^>]*data-superscript-index="0"/,
    );
    expect(html).toMatch(
      /<mark[^>]*data-citation-index="1"[^>]*>[\s\S]*?<\/mark><\/span><button[^>]*data-superscript-index="1"/,
    );
  });

  it("places a mid-sentence citation marker after the sentence it supports", () => {
    const answer = "我们的营收较上年同期增长 8%，主要得益于云服务收入。";
    const citation = companyCitation({ quote: "营收较上年同期增长 8%" });
    const html = render(answer, [citation]);

    expect(html).toMatch(
      /<mark[^>]*>营收较上年同期增长 8%<\/mark>[\s\S]*?主要得益于云服务收入。[\s\S]*?<button[^>]*data-superscript-index="0"/,
    );
  });

  it("numbers a superscript to match its sources-list entry", () => {
    // One numbering, one meaning: the `[2]` in the prose points at the
    // `[2]` row under the answer, never at a second counter.
    //
    // PREMISE CHANGED: the row labels used to read `[1]
    // AAPL-10Q-2024Q3`, so the correspondence was pinned through a
    // document id. The default label is now readable copy and the
    // correspondence is pinned through the number itself, which is the
    // thing the two surfaces must share.
    const html = render(PROSE, twoCitations());
    expect(html).toContain("data-superscript-index=\"0\"");
    expect(html).toContain("[1] 公司资料 · 第 3 页 · 出处 1");
    expect(html).toContain("data-superscript-index=\"1\"");
    expect(html).toContain("[2] 公司资料 · 第 7 页 · 出处 2");
    // …and the raw id is still reachable for a developer.
    expect(renderAs(PROSE, twoCitations(), undefined, true)).toContain(
      "[1] AAPL-10Q-2024Q3",
    );
  });

  it("is keyboard-reachable: a focusable button, not a hover-only title", () => {
    const html = render(PROSE, twoCitations());
    // Tab reach and focus-reveal depend on a real control with a
    // disclosed target; the old hover-only `title` on a non-focusable
    // <mark> reached neither.
    const control = /<button[^>]*data-superscript-index="0"[^>]*>/.exec(html)?.[0] ?? "";
    expect(control).not.toBe("");
    expect(control).toContain('type="button"');
    expect(control).toContain('aria-expanded="false"');
    expect(control).toContain('aria-controls="inline-cite-1"');
    // The excerpt the control reveals is really in the markup, bound to
    // the id the control names.
    expect(html).toMatch(/id="inline-cite-1"/);
  });

  it("keeps the cited span free of a hover-only title attribute", () => {
    const html = render(PROSE, twoCitations());
    expect(html).not.toMatch(/<mark[^>]*\stitle=/);
  });

  it("reveals the cited excerpt through the control it names", () => {
    const html = render(PROSE, twoCitations());
    const revealed = /<span[^>]*id="inline-cite-1"[^>]*>([\s\S]*?)<\/span>/.exec(html);
    expect(revealed).not.toBeNull();
    expect(text(revealed?.[0] ?? "")).toContain(QUOTE_A);
  });

  it("emits no superscript for an answer with no citations", () => {
    expect(superscripts(render("营收同比增长 8%。", []))).toEqual([]);
  });
});

// specs/demo-seed.md item 6: "Each entry carries document name, section
// or subheading, and page number, and is clickable to jump to that
// location." The jump is the host's attachment door — the same address
// the visual-claim rows already link to — reached from the thread the
// turn is rendered on. It is a real ``<a href>``, so a keyboard user
// follows it exactly as a pointer user does.
describe("the sources-list jump to the cited location", () => {
  const THREAD_ID = "t-42";
  // The product route table is pinned data (``App.tsx`` renders the same
  // constants), so the test enters through the product's own path rather
  // than a hardcoded guess at it.
  const THREAD_ROUTE_PATH = PRODUCT_ROUTE_PATHS[1];
  const answer = "渠道库存较上季度下降。";

  function renderInThread(citations: Citation[]): string {
    return renderToString(
      createElement(
        StaticRouter,
        { location: `/t/${THREAD_ID}` },
        createElement(
          Routes,
          null,
          createElement(Route, {
            path: THREAD_ROUTE_PATH,
            element: createElement(AnswerWithCitations, { answer, citations }),
          }),
        ),
      ),
    );
  }

  /** Every rendered jump link, as whole anchor nodes. */
  function jumpNodes(html: string): string[] {
    return (
      html.match(/<a[^>]*data-testid="source-jump"[^>]*>[\s\S]*?<\/a>/g) ?? []
    );
  }

  it("links the row to the host's attachment door for THIS thread and attachment", () => {
    const nodes = jumpNodes(renderInThread([attachmentCitation()]));
    expect(nodes).toHaveLength(1);
    // One door, one spelling: the address a jump names is the one the
    // client's own attachment fetch uses.
    expect(nodes[0]).toContain(
      `href="${threadAttachmentUrl(THREAD_ID, "att-1")}"`,
    );
    expect(nodes[0]).toContain('href="/api/threads/t-42/attachments/att-1"');
  });

  it("carries the entry's own label — document name and locator — inside the link", () => {
    // The link names the place it jumps to; that text is the row's
    // identity, not a separate "here" affordance the reader has to find.
    const node = jumpNodes(renderInThread([attachmentCitation()]))[0] ?? "";
    expect(text(node)).toContain(
      "[1] quarterly-notes.pdf 章节未注明 · p.4",
    );
    // …and it says so, for a reader who only hears the control.
    expect(node).toContain("回到原始位置");
  });

  it("names the evidence-derived section together with the page", () => {
    const html = renderInThread([
      attachmentCitation({ section: "Section 3.2" }),
    ]);
    const node = jumpNodes(html)[0] ?? "";
    const toggle =
      /<button[^>]*aria-controls="source-quote-1"[^>]*>/.exec(html)?.[0] ?? "";

    expect(text(node)).toContain("[1] quarterly-notes.pdf Section 3.2 · p.4");
    expect(toggle).toContain(
      'aria-label="显示引文：[1] quarterly-notes.pdf Section 3.2 · p.4"',
    );
  });

  it("keeps the jump and the quote reveal as two sibling controls, never nested", () => {
    const html = renderInThread([attachmentCitation()]);
    // The link holds its label text and nothing else: an interactive
    // element inside a link is illegal HTML and breaks the keyboard
    // path, so the quote toggle is the row's sibling.
    const inner =
      /<a[^>]*data-testid="source-jump"[^>]*>([\s\S]*?)<\/a>/.exec(html)?.[1] ?? "";
    expect(inner).toContain("quarterly-notes.pdf");
    expect(inner).not.toContain("<");
    expect(jumpNodes(html)[0]).not.toContain("<button");
    // …and no button wraps a link: the reveal and the jump sit side by
    // side in the row, never one inside the other.
    expect(html).not.toMatch(
      /<button[^>]*>(?:(?!<\/button>)[\s\S])*?<a[\s>]/,
    );
  });

  it("keeps the quote reveal reachable: a disclosed button that owns the excerpt", () => {
    const html = renderInThread([attachmentCitation()]);
    const toggle =
      /<button[^>]*aria-controls="source-quote-1"[^>]*>/.exec(html)?.[0] ?? "";
    expect(toggle).not.toBe("");
    expect(toggle).toContain('type="button"');
    expect(toggle).toContain('aria-expanded="false"');
    expect(toggle).toContain(
      'aria-label="显示引文：[1] quarterly-notes.pdf 章节未注明 · p.4"',
    );
    // The panel it controls is in the markup and hidden until opened, so
    // the control always discloses something.
    const panel = /<p[^>]*id="source-quote-1"[^>]*>([\s\S]*?)<\/p>/.exec(html);
    expect(panel?.[0]).toContain("sr-only");
    expect(text(panel?.[0] ?? "")).toContain("渠道库存较上季度下降。");
  });

  it("renders no link at all for a citation with no target — a ledger row is a record, not a page", () => {
    const html = renderInThread([databaseRowCitation()]);
    expect(jumpNodes(html)).toHaveLength(0);
    expect(html).not.toMatch(/<a[\s>]/);
    // The row still reads honestly — identity, snapshot time and excerpt.
    // PREMISE CHANGED: the view name and the ``as of <ISO ts>`` line were
    // default copy; both are now Chinese, and the raw view name is
    // developer-mode copy.
    expect(text(html)).toContain("一条记录");
    expect(text(html)).toContain("数据截至 2026年03月01日");
    expect(text(html)).not.toContain("ledger_ro.contract_expiry");
    expect(html).toContain('aria-controls="source-quote-1"');
  });

  it("never spells a fake destination for a row that cannot jump", () => {
    // A placeholder href reads as "this goes somewhere" and takes the
    // reader nowhere; absent is the honest rendering.
    for (const citation of [databaseRowCitation(), companyCitation()]) {
      const html = renderInThread([citation]);
      expect(html).not.toContain('href="#"');
      expect(html).not.toContain('href=""');
      expect(jumpNodes(html)).toHaveLength(0);
    }
  });

  it("jumps only the rows that have a target, in a mixed list", () => {
    const html = renderInThread([
      attachmentCitation(),
      databaseRowCitation(),
    ]);
    const nodes = jumpNodes(html);
    expect(nodes).toHaveLength(1);
    expect(nodes[0]).toContain("quarterly-notes.pdf");
    // The ledger row beside it is still rendered, just not as a link.
    expect(text(html)).toContain("一条记录");
  });

  it("renders no jump at all outside a thread — an attachment id alone names no address", () => {
    expect(render(answer, [attachmentCitation()])).not.toContain(
      'data-testid="source-jump"',
    );
  });
});

// The whole turn, rendered and read as a reader sees it. The unit-level
// guards live in evidenceContract.test.ts; this is the check that the
// COMPONENT passes all of them through — a label could be safe in isolation
// and reach the page through another path.
describe("the default rendering carries no forbidden label", () => {
  const PROSE = `${QUOTE}${attachmentCitation().quote}`;

  function renderedVisibleText(citations: Citation[], developerMode: boolean): string {
    const html = renderAs(
      PROSE,
      citations,
      [{ citation_index: 2, error: "insufficient_evidence" }],
      developerMode,
    );
    return text(html);
  }

  it("shows no wire field name, tool name, filing format, hex digest or record key", () => {
    const visible = renderedVisibleText(
      [companyCitation(), attachmentCitation({ filename: "" }), databaseRowCitation()],
      false,
    );
    expect(forbiddenDefaultLabels([visible])).toEqual([]);
    // The specific ids this ticket named, so a regression is readable.
    expect(visible).not.toContain("AAPL-10Q-2024Q3");
    expect(visible).not.toContain("ledger_ro");
    expect(visible).not.toContain("contract_id");
    expect(visible).not.toContain("citation gate");
  });

  it("keeps the internal component name out of the standing sources note", () => {
    const html = render(PROSE, [companyCitation()]);
    expect(html).not.toContain("citation gate");
    expect(html).toContain("引文是原文中的匹配片段");
  });

  it("surfaces the raw identity under developer mode, so nothing was deleted", () => {
    const visible = renderedVisibleText(
      [companyCitation(), databaseRowCitation()],
      true,
    );
    expect(visible).toContain("AAPL-10Q-2024Q3");
    expect(visible).toContain("ledger_ro.contract_expiry");
    expect(visible).toContain("insufficient_evidence");
    expect(forbiddenDefaultLabels([visible]).length).toBeGreaterThan(0);
  });
});
