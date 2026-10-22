// SC6 (specs/workbench-retrieve-product-seam.md): the left-corner
// helper reads the same boot snapshot as ``GET /api/status``. For
// 缺 (missing) and 坏 (fault) the operator string is EXACTLY
// 「知识 MCP 故障」; for 可达 (reachable) that string must be absent
// (and so must the retired fake-retrieve 「可用」 copy). The helper is
// plain data, no DOM.

import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  BOUND_COLLECTION_UNBOUND,
  boundCollectionLine,
  KNOWLEDGE_MCP_FAULT_COPY,
  knowledgeMcpLine,
  parseWorkspaceStatus,
} from "./knowledgeMcp";
import type { WorkspaceStatus } from "./types";

/** The host SSOT for the ``GET /api/status`` payload. */
const HOST_WORKSPACE_HTTP_PATH = join(
  dirname(fileURLToPath(import.meta.url)),
  "..",
  "..",
  "src",
  "fin",
  "host",
  "workspace_http.py",
);

const BASE: WorkspaceStatus = {
  knowledge_mcp: "reachable",
  retrieve_adapter_kind: "mcp",
  model_identity: "fin/MiniMax-M3",
  corpus_filing_count: 12,
  bound_collection_name: "默认资料集合",
  run_counts: { active: 2, paused: 1, suspected_stuck: 0 },
};

describe("knowledgeMcpLine", () => {
  it("renders the exact fault copy for the missing state", () => {
    const line = knowledgeMcpLine({ ...BASE, knowledge_mcp: "missing" }, true);
    expect(line.state).toBe("missing");
    expect(line.tone).toBe("degraded");
    expect(line.text).toBe(KNOWLEDGE_MCP_FAULT_COPY);
    expect(line.text).toBe("知识 MCP 故障");
  });

  it("renders the exact fault copy for the fault state", () => {
    const line = knowledgeMcpLine({ ...BASE, knowledge_mcp: "fault" }, true);
    expect(line.state).toBe("fault");
    expect(line.tone).toBe("degraded");
    expect(line.text).toBe(KNOWLEDGE_MCP_FAULT_COPY);
  });

  it("renders a healthy line without the fault copy for reachable", () => {
    const line = knowledgeMcpLine(BASE, true);
    expect(line.state).toBe("reachable");
    expect(line.tone).toBe("ok");
    expect(line.text).not.toContain(KNOWLEDGE_MCP_FAULT_COPY);
    // The retired fake-retrieve healthy word must not sneak back in.
    expect(line.text).not.toContain("故障");
    expect(line.text).not.toContain("可用");
    expect(line.text.length).toBeGreaterThan(0);
  });

  it("renders a neutral line before the status lands (null / not loaded)", () => {
    const loading = knowledgeMcpLine(null, false);
    expect(loading.state).toBe("unknown");
    expect(loading.tone).toBe("neutral");
    expect(loading.text).not.toContain(KNOWLEDGE_MCP_FAULT_COPY);
    expect(loading.text).not.toContain("可用");
  });

  it("stays neutral when the status route never answered", () => {
    const unavailable = knowledgeMcpLine(null, true);
    expect(unavailable.state).toBe("unknown");
    expect(unavailable.tone).toBe("neutral");
    expect(unavailable.text).not.toContain(KNOWLEDGE_MCP_FAULT_COPY);
  });

  it("never renders a blank line in any reachable state", () => {
    // The "never blank" invariant (SC7 of the retired status strip,
    // re-pinned here): missing / fault / reachable / not-loaded /
    // status-route-unavailable each produce non-empty ``text``.
    const lines = [
      knowledgeMcpLine({ ...BASE, knowledge_mcp: "missing" }, true),
      knowledgeMcpLine({ ...BASE, knowledge_mcp: "fault" }, true),
      knowledgeMcpLine(BASE, true),
      knowledgeMcpLine(null, false),
      knowledgeMcpLine(null, true),
    ];
    for (const line of lines) {
      expect(line.text.length, `${line.state} text`).toBeGreaterThan(0);
    }
  });
});

// T4 (specs/first-closed-loop.md): the bound 资料集合 line. A sibling
// of the 知识 MCP line — a collection can be named while the MCP is
// 可达, and the 未绑定 state must read differently from the bound one.
describe("boundCollectionLine", () => {
  it("names the bound collection", () => {
    const line = boundCollectionLine(BASE, true);
    expect(line.state).toBe("bound");
    expect(line.tone).toBe("ok");
    expect(line.text).toBe("资料集合：默认资料集合");
  });

  it("renders the 未绑定 state distinctly from the bound one", () => {
    const bound = boundCollectionLine(BASE, true);
    const unbound = boundCollectionLine(
      { ...BASE, bound_collection_name: BOUND_COLLECTION_UNBOUND },
      true,
    );
    expect(unbound.state).toBe("unbound");
    expect(unbound.text).toBe("资料集合：未绑定");
    expect(unbound.text).not.toBe(bound.text);
    // 未绑定 is a configuration fact, not a fault — no accusation.
    expect(unbound.tone).toBe("neutral");
    expect(unbound.text).not.toContain(KNOWLEDGE_MCP_FAULT_COPY);
  });

  it("never claims 就绪, and never renders the local filing count", () => {
    // SC7: the bind line names the collection. It must not turn the
    // local catalog count into a readiness word — the exact confusion
    // the spec names. Load-bearing on ``BASE.corpus_filing_count``
    // (12) being non-zero.
    expect(BASE.corpus_filing_count).toBeGreaterThan(0);
    const lines = [
      boundCollectionLine(BASE, true),
      boundCollectionLine(
        { ...BASE, bound_collection_name: BOUND_COLLECTION_UNBOUND },
        true,
      ),
      boundCollectionLine(null, true),
    ];
    for (const line of lines) {
      expect(line.text).not.toContain("就绪");
      expect(line.text).not.toContain("12");
      expect(line.text).not.toContain("可用");
    }
  });

  it("renders a neutral unknown line before the status lands", () => {
    const loading = boundCollectionLine(null, false);
    expect(loading.state).toBe("unknown");
    expect(loading.tone).toBe("neutral");
    // Not-yet-loaded is distinct from 未绑定: the boot has not said.
    expect(loading.text).not.toContain("未绑定");
  });

  it("renders unknown, not 未绑定, when the status route never answered", () => {
    const unavailable = boundCollectionLine(null, true);
    expect(unavailable.state).toBe("unknown");
    expect(unavailable.text).not.toContain("未绑定");
  });

  it("never renders a blank line in any state", () => {
    const lines = [
      boundCollectionLine(BASE, true),
      boundCollectionLine(
        { ...BASE, bound_collection_name: BOUND_COLLECTION_UNBOUND },
        true,
      ),
      boundCollectionLine(null, false),
      boundCollectionLine(null, true),
    ];
    for (const line of lines) {
      expect(line.text.length, `${line.state} text`).toBeGreaterThan(0);
    }
  });

  it("renders the wire name verbatim, holding no copy of the name space", () => {
    // The SPA must not carry the iknow literal: a host-side rename has
    // to move this line without an SPA change.
    const line = boundCollectionLine(
      { ...BASE, bound_collection_name: "某资料集合" },
      true,
    );
    expect(line.text).toContain("某资料集合");
  });
});

describe("parseWorkspaceStatus", () => {
  it("accepts the pinned /api/status fields including run counts", () => {
    expect(parseWorkspaceStatus({ ...BASE })).toEqual(BASE);
  });

  it("accepts zero run counts as valid values", () => {
    const status = {
      ...BASE,
      run_counts: { active: 0, paused: 0, suspected_stuck: 0 },
    };
    expect(parseWorkspaceStatus(status)).toEqual(status);
  });

  it("rejects missing or incomplete run counts", () => {
    const { run_counts: _dropped, ...partial } = BASE;
    expect(parseWorkspaceStatus(partial)).toBeNull();
    expect(parseWorkspaceStatus({ ...BASE, run_counts: null })).toBeNull();
    expect(parseWorkspaceStatus({ ...BASE, run_counts: [] })).toBeNull();
    for (const missingField of ["active", "paused", "suspected_stuck"] as const) {
      const runCounts = { ...BASE.run_counts };
      delete runCounts[missingField];
      expect(
        parseWorkspaceStatus({ ...BASE, run_counts: runCounts }),
        `${missingField} is required`,
      ).toBeNull();
    }
  });

  it("rejects negative, non-integer, unsafe, and non-finite run counts", () => {
    const invalidCounts: unknown[] = [
      -1,
      1.5,
      Number.MAX_SAFE_INTEGER + 1,
      Number.NaN,
      Number.POSITIVE_INFINITY,
      "1",
      true,
    ];

    for (const field of ["active", "paused", "suspected_stuck"] as const) {
      for (const value of invalidCounts) {
        expect(
          parseWorkspaceStatus({
            ...BASE,
            run_counts: { ...BASE.run_counts, [field]: value },
          }),
          `${field} rejects ${String(value)}`,
        ).toBeNull();
      }
    }
  });

  it("ignores unknown extra keys rather than failing the payload", () => {
    const parsed = parseWorkspaceStatus({ ...BASE, future_field: 1 });
    expect(parsed).toEqual(BASE);
  });

  it("rejects a non-object payload", () => {
    expect(parseWorkspaceStatus(null)).toBeNull();
    expect(parseWorkspaceStatus(undefined)).toBeNull();
    expect(parseWorkspaceStatus("reachable")).toBeNull();
  });

  it("rejects a payload missing a pinned field", () => {
    const { corpus_filing_count: _dropped, ...partial } = BASE;
    expect(parseWorkspaceStatus(partial)).toBeNull();
    const { knowledge_mcp: _mcp, ...noMcp } = BASE;
    expect(parseWorkspaceStatus(noMcp)).toBeNull();
  });

  it("rejects an unknown knowledge_mcp value instead of coercing it", () => {
    expect(
      parseWorkspaceStatus({ ...BASE, knowledge_mcp: "reachable-fake" }),
    ).toBeNull();
    expect(
      parseWorkspaceStatus({ ...BASE, knowledge_mcp: "unreachable" }),
    ).toBeNull();
  });

  it("rejects the retired retrieve_reachability field", () => {
    // The old two-state payload must not be coerced onto the new
    // three-state field; a parser that vouches for a payload it
    // cannot read is worse than one that says unknown.
    const { knowledge_mcp: _mcp, ...legacy } = {
      ...BASE,
      retrieve_reachability: "reachable",
    };
    expect(parseWorkspaceStatus(legacy)).toBeNull();
    expect(
      parseWorkspaceStatus({ ...BASE, retrieve_reachability: "reachable" }),
    ).toBeNull();
  });

  it("rejects an unknown retrieve adapter kind", () => {
    expect(
      parseWorkspaceStatus({ ...BASE, retrieve_adapter_kind: "bm25" }),
    ).toBeNull();
  });

  it("rejects a payload missing the bind field", () => {
    const { bound_collection_name: _dropped, ...partial } = BASE;
    expect(parseWorkspaceStatus(partial)).toBeNull();
  });

  it("rejects an empty bind name instead of rendering a blank line", () => {
    expect(
      parseWorkspaceStatus({ ...BASE, bound_collection_name: "" }),
    ).toBeNull();
    expect(
      parseWorkspaceStatus({ ...BASE, bound_collection_name: null }),
    ).toBeNull();
  });

  it("keeps the 未绑定 sentinel parseable as a real value", () => {
    expect(
      parseWorkspaceStatus({
        ...BASE,
        bound_collection_name: BOUND_COLLECTION_UNBOUND,
      }),
    ).toEqual({ ...BASE, bound_collection_name: BOUND_COLLECTION_UNBOUND });
  });

  it("keeps the wire mirror in lockstep with the host response model", () => {
    // The parser REQUIRES every known field, so the two sides have to
    // move together: a host-side field addition that skips this file
    // makes ``parseWorkspaceStatus`` reject every real payload and the
    // left corner degrade to 未知 with no test failing anywhere else.
    // Same cross-language source guard this repo already runs in
    // ``comparisonStream.test.ts``, and deliberately NOT a name list:
    // a list would have to be edited in the same commit as the drift
    // it is meant to catch.
    const hostSource = readFileSync(HOST_WORKSPACE_HTTP_PATH, "utf8");
    const test = readFileSync(fileURLToPath(import.meta.url), "utf8");

    for (const field of Object.keys(BASE)) {
      expect(hostSource, `${field} missing from the host response`).toContain(
        `${field}:`,
      );
      expect(test, `${field} unpinned in the parser cases`).toContain(field);
    }
  });

  it("pins the same field set as the host response model", () => {
    const hostSource = readFileSync(HOST_WORKSPACE_HTTP_PATH, "utf8");
    const body = hostSource.slice(
      hostSource.indexOf("class WorkspaceStatusResponse(BaseModel):"),
    );
    const fields = new Set(
      body
        .slice(0, body.indexOf("\nclass "))
        .split("\n")
        .map((line) => /^ {4}([a-z_]+): /.exec(line)?.[1])
        .filter((name): name is string => name !== undefined),
    );

    expect(fields.size).toBeGreaterThan(0);
    expect(fields).toEqual(new Set(Object.keys(BASE)));
  });
});
