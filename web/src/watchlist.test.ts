import { describe, expect, it } from "vitest";
import {
  noticeSummary,
  parseFormsInput,
  parseNotices,
  parseWatchlist,
  unreadCount,
  watchSummary,
} from "./watchlist";

describe("parseFormsInput", () => {
  it("splits on commas and whitespace and upper-cases", () => {
    expect(parseFormsInput("10-k, 8-K  10-q")).toEqual(["10-K", "8-K", "10-Q"]);
  });

  it("drops blanks and duplicates so one form is watched once", () => {
    expect(parseFormsInput(" , 10-K ,10-k,, ")).toEqual(["10-K"]);
  });

  it("returns an empty list for empty input (watch every form)", () => {
    expect(parseFormsInput("   ")).toEqual([]);
  });
});

describe("watchSummary", () => {
  const base = { id: "w1", issuer: "AAPL", created_at: "2026-08-27T00:00:00Z" };

  it("lists the watched form types", () => {
    expect(watchSummary({ ...base, forms: ["10-K", "8-K"] })).toBe("10-K、8-K");
  });

  it("says every form when the watch named none", () => {
    expect(watchSummary({ ...base, forms: [] })).toBe("全部 form");
  });
});

describe("noticeSummary", () => {
  it("names the filing that entered the local catalog", () => {
    const text = noticeSummary({
      id: "n1",
      watch_id: "w1",
      issuer: "AAPL",
      form: "10-K",
      document_id: "AAPL-2023FY",
      period: "2023 FY",
      created_at: "2026-08-27T00:00:00Z",
      read: false,
    });
    expect(text).toContain("AAPL-2023FY");
    expect(text).toContain("10-K");
  });
});

describe("parseWatchlist", () => {
  it("keeps well-formed rows and drops malformed ones", () => {
    const parsed = parseWatchlist([
      {
        id: "w1",
        issuer: "AAPL",
        forms: ["10-K"],
        created_at: "2026-08-27T00:00:00Z",
      },
      { id: "w2" },
      "nonsense",
    ]);
    expect(parsed.map((w) => w.id)).toEqual(["w1"]);
    expect(parsed[0].forms).toEqual(["10-K"]);
  });

  it("treats a non-array payload as an empty watchlist", () => {
    expect(parseWatchlist({ watchlist: [] })).toEqual([]);
  });
});

describe("parseNotices", () => {
  it("defaults the read flag to false", () => {
    const parsed = parseNotices([
      {
        id: "n1",
        watch_id: "w1",
        issuer: "AAPL",
        form: "10-K",
        document_id: "AAPL-2023FY",
        period: "2023 FY",
        created_at: "2026-08-27T00:00:00Z",
      },
    ]);
    expect(parsed[0].read).toBe(false);
  });
});

describe("unreadCount", () => {
  it("counts only the notices the operator has not dismissed", () => {
    const notice = {
      id: "n1",
      watch_id: "w1",
      issuer: "AAPL",
      form: "10-K",
      document_id: "AAPL-2023FY",
      period: "2023 FY",
      created_at: "2026-08-27T00:00:00Z",
      read: false,
    };
    expect(
      unreadCount([notice, { ...notice, id: "n2", read: true }]),
    ).toBe(1);
  });
});
