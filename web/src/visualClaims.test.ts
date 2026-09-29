// The visual-observation lane's presentation rules, pinned without a DOM:
// what a claim links to, how its page / region / provenance read, and the
// one invariant the whole lane exists for — a generated visual
// observation is never a source quote (spec
// thread-attachment-source-read lines 58 and 65).

import { describe, expect, it } from "vitest";

import { attachmentSourceRef, threadAttachmentUrl } from "./sourceLocator";
import {
  VISUAL_CLAIMS_HEADING,
  VISUAL_NOT_A_QUOTE_NOTE,
  parseVisualClaim,
  parseVisualClaimFrame,
  parseVisualClaims,
  turnVisualClaims,
  visualClaimCallsLabel,
  visualClaimEvidenceLabel,
  visualClaimPageLabel,
  visualClaimProvenanceLabel,
  visualClaimScopeLabel,
  visualClaimSourceRef,
  visualClaimSourceUrl,
  visualClaimTargetLabel,
} from "./visualClaims";
import type { Citation, Turn, VisualClaim } from "./types";

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

function turnWith(visual_claims?: VisualClaim[]): Turn {
  return {
    run_id: "22222222-2222-4222-8222-222222222222",
    question: "图表里服务业务收入是多少？",
    status: "done",
    answer: "见图表。",
    citations: [],
    visual_claims,
    created_at: "2026-01-01T00:00:00.000Z",
  };
}

describe("turnVisualClaims", () => {
  it("reads an empty turn as no claims", () => {
    expect(turnVisualClaims(turnWith([]))).toEqual([]);
  });

  it("treats a peer that predates the lane as no claims", () => {
    expect(turnVisualClaims(turnWith(undefined))).toEqual([]);
  });

  it("hands the rows through untouched — no derived text, no reordering", () => {
    const claims = [visualClaim(), visualClaim({ attachment_id: "att-2" })];
    expect(turnVisualClaims(turnWith(claims))).toEqual(claims);
  });
});

describe("the copy says observation, never quote", () => {
  it("names the surface a generated visual observation", () => {
    expect(VISUAL_CLAIMS_HEADING).toContain("视觉观察");
    expect(VISUAL_CLAIMS_HEADING).toContain("非原文引用");
  });

  it("carries a note that rules out a verbatim quote", () => {
    expect(VISUAL_NOT_A_QUOTE_NOTE).toContain("不是原文引用");
  });

  it("renders no quote text for any row label", () => {
    // The row has no quote to render; a label that wrapped the target
    // in quote marks would read back to the operator as a source span.
    const claim = visualClaim();
    for (const label of [
      visualClaimPageLabel(claim),
      visualClaimTargetLabel(claim),
      visualClaimProvenanceLabel(claim),
      visualClaimCallsLabel(claim),
      visualClaimEvidenceLabel(claim),
      visualClaimScopeLabel(claim.scope),
    ]) {
      expect(label).not.toMatch(/[“”"']/);
    }
  });
});

describe("the page a visual claim reads", () => {
  it("labels the physical page as a page", () => {
    expect(visualClaimPageLabel(visualClaim({ page: 7 }))).toBe("p.7");
  });

  it("never renders a visual claim as a bare unit", () => {
    // ``unit`` is the locator ordinal the source opens at; what the
    // operator is being told about is the page that was imaged.
    expect(visualClaimPageLabel(visualClaim({ page: 7, unit: 9 }))).toBe("p.7");
  });
});

describe("the region that was read", () => {
  it("says whole page when no reliable box existed", () => {
    expect(visualClaimScopeLabel(null)).toContain("整页");
  });

  it("prints the normalized bounds it was given", () => {
    expect(visualClaimScopeLabel([0.12, 0.05, 0.41, 0.62])).toBe(
      "区域 0.12, 0.05, 0.41, 0.62",
    );
  });

  it("keeps the bound order left, top, right, bottom", () => {
    const label = visualClaimScopeLabel([0.1, 0.2, 0.3, 0.4]);
    expect(label).toBe("区域 0.10, 0.20, 0.30, 0.40");
  });
});

describe("reused vs newly inspected", () => {
  it("labels a stored observation this run reused", () => {
    expect(visualClaimProvenanceLabel(visualClaim({ reused: true }))).toContain(
      "复用",
    );
  });

  it("labels a fresh inspection as not a reuse", () => {
    const label = visualClaimProvenanceLabel(visualClaim({ reused: false }));
    expect(label).not.toContain("复用");
  });

  it("counts the outbound attempts, and says zero when it reused", () => {
    expect(visualClaimCallsLabel(visualClaim({ outbound_calls: 8 }))).toBe(
      "视觉调用 8 次",
    );
    expect(visualClaimCallsLabel(visualClaim({ outbound_calls: 0 }))).toBe(
      "视觉调用 0 次",
    );
  });

  it("separates the reuse claim from the call count", () => {
    // One reused row and one freshly-inspected row with a different
    // attempt count must not collapse into the same text.
    const reused = `${visualClaimProvenanceLabel(
      visualClaim({ reused: true, outbound_calls: 0 }),
    )}|${visualClaimCallsLabel(visualClaim({ reused: true, outbound_calls: 0 }))}`;
    const fresh = `${visualClaimProvenanceLabel(
      visualClaim({ reused: false, outbound_calls: 1 }),
    )}|${visualClaimCallsLabel(visualClaim({ reused: false, outbound_calls: 1 }))}`;
    expect(reused).not.toBe(fresh);
  });
});

describe("provenance stamps", () => {
  it("names the observation version, provider and model version", () => {
    const label = visualClaimEvidenceLabel(
      visualClaim({
        observation_version: 3,
        provider: "configured-vision",
        model_version: "vision-model-1",
      }),
    );
    expect(label).toContain("v3");
    expect(label).toContain("configured-vision");
    expect(label).toContain("vision-model-1");
  });
});

describe("the link back to the original location", () => {
  it("carries the attachment source_ref — id, version and unit", () => {
    expect(visualClaimSourceRef(visualClaim())).toEqual({
      kind: "attachment",
      attachment_id: "att-1",
      version: "3",
      unit: 4,
    });
  });

  it("builds it through the shared locator helper", () => {
    const claim = visualClaim();
    expect(visualClaimSourceRef(claim)).toEqual(attachmentSourceRef(claim));
  });

  it("links to the host's attachment door for THIS thread and id", () => {
    expect(visualClaimSourceUrl("t h/1", visualClaim())).toBe(
      "/api/threads/t%20h%2F1/attachments/att-1",
    );
  });

  it("quotes an attachment id that would otherwise escape the path", () => {
    expect(visualClaimSourceUrl("t1", visualClaim({ attachment_id: "a/b" }))).toBe(
      "/api/threads/t1/attachments/a%2Fb",
    );
  });

  it("lands on the one attachment door address the client builds", () => {
    // The lane must not keep a second spelling of the door path: the
    // attachment fetch and the source link have to name the same address,
    // or a link could point where the client never sends a request.
    const claim = visualClaim();
    expect(visualClaimSourceUrl("t1", claim)).toBe(
      threadAttachmentUrl("t1", claim.attachment_id),
    );
  });
});

describe("the target the observation answers", () => {
  it("shows the exact chart / series / category target", () => {
    expect(visualClaimTargetLabel(visualClaim({ detail: "营收图 / 服务 / FY24" })))
      .toBe("目标：营收图 / 服务 / FY24");
  });
});

describe("parseVisualClaim — the wire is unknown", () => {
  it("accepts a complete row", () => {
    expect(parseVisualClaim(visualClaim())).toEqual(visualClaim());
  });

  it("accepts a whole-page row whose scope is null", () => {
    const row = { ...visualClaim(), scope: null };
    expect(parseVisualClaim(row)?.scope).toBeNull();
  });

  it("rejects a row that is not a record", () => {
    for (const value of [null, undefined, "att-1", 7, [], ["x"]]) {
      expect(parseVisualClaim(value)).toBeNull();
    }
  });

  it("rejects a row from another channel", () => {
    expect(parseVisualClaim({ ...visualClaim(), kind: "citation" })).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), kind: "visual_observation_v2" }),
    ).toBeNull();
  });

  it("rejects a row missing the identity the link is built from", () => {
    expect(parseVisualClaim({ ...visualClaim(), attachment_id: "" })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), attachment_id: 12 })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), version: 3 })).toBeNull();
  });

  it("rejects a row missing the page or unit the label reads", () => {
    expect(parseVisualClaim({ ...visualClaim(), page: 0 })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), page: 1.5 })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), page: "4" })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), unit: 0 })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), unit: null })).toBeNull();
  });

  it("rejects a row that cannot state what it observed", () => {
    expect(parseVisualClaim({ ...visualClaim(), detail: "" })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), detail: 7 })).toBeNull();
  });

  it("rejects a row that cannot state its own provenance", () => {
    expect(
      parseVisualClaim({ ...visualClaim(), page_image_sha256: undefined }),
    ).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), provider: 1 })).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), observation_version: "1" }),
    ).toBeNull();
  });

  it("rejects a reuse flag or call count it cannot render", () => {
    expect(parseVisualClaim({ ...visualClaim(), reused: "true" })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), reused: 1 })).toBeNull();
    expect(parseVisualClaim({ ...visualClaim(), outbound_calls: 1.5 })).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), outbound_calls: undefined }),
    ).toBeNull();
  });

  it("rejects a region that is not four normalized bounds", () => {
    expect(parseVisualClaim({ ...visualClaim(), scope: [0.1, 0.2] })).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), scope: [0.1, 0.2, 0.3, 1.4] }),
    ).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), scope: [0.1, 0.2, 0.3, -0.1] }),
    ).toBeNull();
    expect(
      parseVisualClaim({ ...visualClaim(), scope: [0.1, 0.2, 0.3, "0.4"] }),
    ).toBeNull();
  });

  it("keeps a whole-page region at the bounds", () => {
    const row = parseVisualClaim({ ...visualClaim(), scope: [0, 0, 1, 1] });
    expect(row?.scope).toEqual([0, 0, 1, 1]);
  });
});

describe("the projector keeps exactly the claim row's key set", () => {
  // The host's row IS these fourteen keys in this order
  // (``VISUAL_CLAIM_FIELDS``). Pinned here as an independent expectation,
  // because the projection — not the ``as`` cast — is what stops a row
  // arriving on the visual channel from carrying anything else, a
  // verbatim ``quote`` most of all.
  const CLAIM_KEYS = [
    "kind",
    "attachment_id",
    "version",
    "unit",
    "page",
    "observation_version",
    "scope",
    "provider",
    "model_version",
    "page_image_sha256",
    "detail",
    "filename",
    "reused",
    "outbound_calls",
  ];

  it("projects the fourteen keys in the host's order", () => {
    expect(Object.keys(parseVisualClaim(visualClaim()) ?? {})).toEqual(
      CLAIM_KEYS,
    );
  });

  it("drops a quote a row should never have carried", () => {
    // The hazard made structural: even a peer that shipped prose alongside
    // the provenance cannot hand the chip layer a span to place.
    const projected = parseVisualClaim({
      ...visualClaim(),
      quote: "营收同比增长 8%。",
      answer_text: "整段观察描述",
    });
    expect(projected).toEqual(visualClaim());
    expect(Object.keys(projected ?? {})).toEqual(CLAIM_KEYS);
    expect((projected as unknown as Partial<Citation>).quote).toBeUndefined();
  });
});

describe("parseVisualClaims / parseVisualClaimFrame", () => {
  it("returns no rows for a list that is not a list", () => {
    expect(parseVisualClaims(undefined)).toEqual([]);
    expect(parseVisualClaims("not-a-list")).toEqual([]);
    expect(parseVisualClaims({ visual_claims: [] })).toEqual([]);
  });

  it("drops the malformed rows and keeps the readable ones", () => {
    const rows = parseVisualClaims([
      visualClaim(),
      { kind: "citation" },
      null,
      visualClaim({ attachment_id: "", page: 3 }),
    ]);
    expect(rows.length).toBe(1);
    expect(rows[0]?.attachment_id).toBe("att-1");
  });

  it("drops a row whose cost or raster identity cannot be true", () => {
    // The raster hash is what ties a claim to one page image, and a
    // negative call count is a cost no provider can report. A row that
    // carries either renders a provenance line the operator would read
    // as evidence, so it is dropped rather than shown half-formed.
    expect(parseVisualClaims([visualClaim({ outbound_calls: -3 })])).toEqual([]);
    expect(parseVisualClaims([visualClaim({ page_image_sha256: "" })])).toEqual(
      [],
    );
    expect(parseVisualClaims([visualClaim({ page_image_sha256: "   " })])).toEqual(
      [],
    );
  });

  it("reads the frame body the host sends", () => {
    expect(parseVisualClaimFrame({ visual_claims: [visualClaim()] })).toEqual([
      visualClaim(),
    ]);
  });

  it("treats an empty frame body as no claims", () => {
    expect(parseVisualClaimFrame({ visual_claims: [] })).toEqual([]);
  });

  it("treats a frame with no such key, or no body at all, as no claims", () => {
    expect(parseVisualClaimFrame({})).toEqual([]);
    expect(parseVisualClaimFrame(null)).toEqual([]);
    expect(parseVisualClaimFrame("visual_claims")).toEqual([]);
    expect(parseVisualClaimFrame({ visual_claims: "att-1" })).toEqual([]);
  });
});

describe("turnVisualClaims re-parses the row", () => {
  it("drops a garbage entry a persisted history row carried", () => {
    // GET history is unvalidated JSON: the same rules must hold after a
    // reload as on the live frame.
    const turn = turnWith([{ kind: "citation" } as unknown as VisualClaim]);
    expect(turnVisualClaims(turn)).toEqual([]);
  });
});
