// SC6 (specs/workbench-retrieve-product-seam.md): the left-corner
// helper reads the same boot snapshot as ``GET /api/status``. For
// 缺 (missing) and 坏 (fault) the operator string is EXACTLY
// 「知识 MCP 故障」; for 可达 (reachable) that string must be absent
// (and so must the retired fake-retrieve 「可用」 copy). The helper is
// plain data, no DOM.

import { describe, expect, it } from "vitest";
import {
  KNOWLEDGE_MCP_FAULT_COPY,
  knowledgeMcpLine,
  parseWorkspaceStatus,
} from "./knowledgeMcp";
import type { WorkspaceStatus } from "./types";

const BASE: WorkspaceStatus = {
  knowledge_mcp: "reachable",
  retrieve_adapter_kind: "mcp",
  model_identity: "fin/MiniMax-M3",
  corpus_filing_count: 12,
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

describe("parseWorkspaceStatus", () => {
  it("accepts the four pinned /api/status fields", () => {
    expect(parseWorkspaceStatus({ ...BASE })).toEqual(BASE);
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
});
