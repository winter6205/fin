// Pure view rules for the generic catalog surface (T6): what the
// operator reads as a document list, and what a publication warning is
// allowed to say.
//
// The warning rules exist because the acceptance is "present safe
// warning state, never raw exception text". The host sends a CLOSED
// vocabulary (a code, an optional typed failure classification, an
// attempt number) and this module turns exactly that into copy. Nothing
// here reads a server-supplied sentence, so a path, a DSN or an
// exception message cannot reach the screen even if one were persisted.

import type { CatalogDimension, CatalogOverview, CatalogWarning } from "./types";

/** What the overview panel renders: a count, the titles the operator
 * reads, and whichever grouping the host qualified. */
export interface CatalogOverviewView {
  count: number;
  /** Present ONLY when no dimension qualifies — the flat fallback. A
   * qualifying dimension replaces it rather than joining it, so the two
   * readings are never shown at once. */
  titles: string[];
  dimensions: CatalogDimension[];
}

/**
 * The documents are visible by TITLE: an ordinary upload has no company
 * or period, so the identity slot is not what a human reads.
 */
export function describeCatalogOverview(
  overview: CatalogOverview | null,
): CatalogOverviewView {
  if (!overview) return { count: 0, titles: [], dimensions: [] };
  const dimensions = overview.dimensions;
  return {
    count: overview.documents.length,
    titles: dimensions.length === 0 ? overview.flat_titles : [],
    dimensions,
  };
}

/** One warning line: the safe sentence, the latest attempt number, and
 * the typed classification that routes the operator — or ``null`` for the
 * fields a pending warning has none of. */
export interface CatalogWarningView {
  code: string;
  message: string;
  attempts: number;
  detail: string | null;
}

/**
 * Safe copy per warning code. Fixed sentences: they name the situation,
 * not the server. The pending line must say the publication is
 * unfinished and recoverable (the row is durable and the retry is
 * idempotent); the failed line must say the same without claiming the
 * push was lost — it stands, and the retry re-publishes it.
 */
export const CATALOG_WARNING_COPY: Record<
  CatalogWarning["code"] | string,
  string
> = {
  catalog_publication_pending: "目录登记还没完成，正在等待重试；正文已经可以检索。",
  catalog_publication_failed: "目录登记失败，正在等待重试；正文已经可以检索，重复入库是安全的。",
  // A code outside the table still renders something: a blank line would
  // read as "nothing wrong" to an operator who has a real problem.
  catalog_publication_unknown: "目录登记有待处理的问题，请查看入库记录。",
};

/** One warning line, or ``null`` once a success is recorded. */
export function describeCatalogWarning(
  warning: CatalogWarning | null | undefined,
): CatalogWarningView | null {
  if (!warning) return null;
  const message =
    CATALOG_WARNING_COPY[warning.code] ??
    CATALOG_WARNING_COPY["catalog_publication_unknown"];
  // The typed classification is a closed vocabulary, so it is safe to
  // show verbatim — and it is what tells the operator WHICH stage to act
  // on. A pending warning carries none.
  const classification =
    warning.failure_code && warning.failure_stage
      ? `${warning.failure_code} / ${warning.failure_stage}`
      : null;
  return {
    code: warning.code,
    message,
    attempts: warning.attempts,
    detail: classification,
  };
}
