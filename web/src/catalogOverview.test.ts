// The generic catalog OVERVIEW view rules (T6), kept pure so the node-env
// suite can pin them without a DOM — the same posture the panel itself
// takes with describeGraphReadiness.
//
// Three rules carry the acceptance:
//   * documents are visible by TITLE, never by a financial identity a
//     metadata-free upload does not have;
//   * only a qualifying dimension is grouped, 未分类 holds the rows with
//     no value for it, and the flat title list is the fallback when
//     NOTHING qualifies;
//   * the publication warning renders SAFE copy from its closed code
//     vocabulary — never an exception message, a path or a DSN, and never
//     a count presented as an attempt total.

import { describe, expect, it } from "vitest";
import {
  CATALOG_WARNING_COPY,
  describeCatalogWarning,
  describeCatalogOverview,
  type CatalogOverviewView,
} from "./catalogOverview";
import type { CatalogDocument, CatalogOverview, CatalogWarning } from "./types";

function document(overrides: Partial<CatalogDocument> = {}): CatalogDocument {
  return {
    document_id: "doc_a",
    version: "v1",
    title: "Team Handbook",
    doc_type: "narrative",
    page_count: 3,
    ...overrides,
  };
}

function overview(overrides: Partial<CatalogOverview> = {}): CatalogOverview {
  return { documents: [], dimensions: [], flat_titles: [], ...overrides };
}

const FAILED: CatalogWarning = {
  code: "catalog_publication_failed",
  attempts: 2,
  failure_code: "catalog_unavailable",
  failure_stage: "lock",
};

const PENDING: CatalogWarning = { code: "catalog_publication_pending", attempts: 1, stage: "recovery" };

describe("describeCatalogOverview", () => {
  it("counts the documents and lists their titles in display order", () => {
    const view = describeCatalogOverview(
      overview({
        documents: [
          document({ document_id: "doc_b", title: "Runbook" }),
          document({ document_id: "doc_a", title: "Handbook" }),
        ],
        flat_titles: ["Handbook", "Runbook"],
      }),
    );

    expect(view.count).toBe(2);
    expect(view.titles).toEqual(["Handbook", "Runbook"]);
  });

  it("shows a real dimension with its buckets", () => {
    const view = describeCatalogOverview(
      overview({
        documents: [document({ document_id: "doc_1" }), document({ document_id: "doc_2" })],
        dimensions: [
          {
            key: "doc_type",
            label: "文档类型",
            groups: [
              { value: "manual", document_ids: ["doc_2"] },
              { value: "narrative", document_ids: ["doc_1"] },
            ],
          },
        ],
      }),
    );

    expect(view.dimensions).toEqual([
      {
        key: "doc_type",
        label: "文档类型",
        groups: [
          { value: "manual", document_ids: ["doc_2"] },
          { value: "narrative", document_ids: ["doc_1"] },
        ],
      },
    ]);
    // A qualifying dimension is the reading; the title list is only the
    // fallback, so it is NOT rendered beside a real grouping.
    expect(view.titles).toEqual([]);
  });

  it("keeps 未分类 visible as its own bucket rather than dropping the rows", () => {
    const view = describeCatalogOverview(
      overview({
        documents: [document({ document_id: "doc_2" })],
        dimensions: [
          {
            key: "company",
            label: "公司",
            groups: [
              { value: "Acme", document_ids: ["doc_1"] },
              { value: "未分类", document_ids: ["doc_2"] },
            ],
          },
        ],
      }),
    );

    const unclassified = view.dimensions[0].groups.find(
      (g) => g.value === "未分类",
    );
    expect(unclassified?.document_ids).toEqual(["doc_2"]);
  });

  it("reports an empty corpus as empty rather than claiming documents exist", () => {
    const view = describeCatalogOverview(overview());
    expect(view.count).toBe(0);
    expect(view.titles).toEqual([]);
    expect(view.dimensions).toEqual([]);
  });
});

describe("describeCatalogWarning", () => {
  it("has no line at all when the publication settled", () => {
    expect(describeCatalogWarning(null)).toBeNull();
  });

  it("renders the SAFE copy for a pending recovery warning", () => {
    const view = describeCatalogWarning(PENDING);
    expect(view?.message).toBe(CATALOG_WARNING_COPY["catalog_publication_pending"]);
    expect(view?.attempts).toBe(1);
  });

  it("renders the SAFE copy for a typed failed warning", () => {
    const view = describeCatalogWarning(FAILED);
    expect(view?.message).toBe(CATALOG_WARNING_COPY["catalog_publication_failed"]);
    expect(view?.attempts).toBe(2);
  });

  it("names the closed failure classification and never an exception string", () => {
    const view = describeCatalogWarning(FAILED);
    // The closed vocabulary is shown because it is what routes the
    // operator; it is a fixed token, not a server string.
    expect(view?.detail).toBe("catalog_unavailable / lock");
    expect(view?.detail).not.toContain("/home");
    expect(view?.message).not.toContain("catalog_unavailable");
  });

  it("carries no detail when the warning has no typed classification", () => {
    expect(describeCatalogWarning(PENDING)?.detail).toBeNull();
  });

  it("states the attempt count as the LATEST attempt, not a total", () => {
    expect(describeCatalogWarning(FAILED)?.attempts).toBe(2);
    expect(describeCatalogWarning({ ...FAILED, attempts: 7 })?.attempts).toBe(7);
  });

  it("survives a code outside the copy table with non-empty copy", () => {
    // The wire type is closed, but a newer peer must not render blank.
    const unknown = { code: "catalog_publication_unknown", attempts: 1 };
    const view = describeCatalogWarning(unknown as unknown as CatalogWarning);
    expect(view?.message.length).toBeGreaterThan(0);
  });
});

describe("the safe-copy table itself", () => {
  it("carries a line for every warning code the wire can send", () => {
    for (const code of ["catalog_publication_pending", "catalog_publication_failed"] as const) {
      expect(CATALOG_WARNING_COPY[code].length).toBeGreaterThan(0);
      expect(CATALOG_WARNING_COPY[code]).not.toMatch(/[/\\]|Exception|Traceback/);
    }
  });
});

/** The view type the panel consumes; named so a drift is a type error. */
const _viewTypeIsUsed: CatalogOverviewView | null = null;
void _viewTypeIsUsed;
