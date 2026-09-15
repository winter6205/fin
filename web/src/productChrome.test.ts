// SC8 (specs/workbench-retrieve-product-seam.md): source-level guard
// over the product chrome. ``App.tsx`` must not route the retired
// ``/kb`` / ``/monitor`` pages, the Sidebar must carry no nav chrome for
// them, and the product source must not carry the retired fake-retrieve
// health copy. Runs in node-env vitest and reads the tree via
// ``node:fs`` — no DOM, no bundler transform.
//
// Scope is deliberately narrow: ``*.test.ts`` files are skipped (they
// legitimately name retired strings to assert their rejection), and
// the bare word 「可用」 is only forbidden on the chrome files that
// render MCP health — ingest failure copy elsewhere uses it for
// unrelated reasons.

import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";
import { PRODUCT_ROUTE_PATHS } from "./productRoutes";

const SRC_DIR = dirname(fileURLToPath(import.meta.url));
const APP_PATH = join(SRC_DIR, "App.tsx");
const SIDEBAR_PATH = join(SRC_DIR, "components", "Sidebar.tsx");
const COMPOSER_PATH = join(SRC_DIR, "components", "Composer.tsx");
const THREAD_PAGE_PATH = join(SRC_DIR, "pages", "ThreadPage.tsx");
/** The host SSOT for the shared 409 token + copy. */
const HOST_KNOWLEDGE_MCP_PATH = join(
  SRC_DIR,
  "..",
  "..",
  "src",
  "fin",
  "host",
  "knowledge_mcp.py",
);

const RETIRED_FAKE_HEALTH_COPY = ["reachable-fake", "演示搜索"];
const CHROME_HEALTH_FILES = [
  "App.tsx",
  join("components", "Sidebar.tsx"),
  "knowledgeMcp.ts",
];

function collectSourceFiles(dir: string): string[] {
  const out: string[] = [];
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) {
      out.push(...collectSourceFiles(full));
    } else if (
      !entry.name.endsWith(".test.ts") &&
      (entry.name.endsWith(".ts") || entry.name.endsWith(".tsx"))
    ) {
      out.push(full);
    }
  }
  return out;
}

describe("product route table", () => {
  it("is exactly the home and thread patterns", () => {
    // The real SSOT ``App.tsx`` destructures — not a second matcher.
    expect([...PRODUCT_ROUTE_PATHS]).toEqual(["/", "/t/:thread_id"]);
  });
});

describe("App.tsx product chrome", () => {
  const source = readFileSync(APP_PATH, "utf8");

  it("does not route the retired /kb or /monitor pages", () => {
    expect(source).not.toContain('"/kb"');
    expect(source).not.toContain('"/monitor"');
  });

  it("does not import KnowledgeBasePage or MonitorPage", () => {
    expect(source).not.toContain("KnowledgeBasePage");
    expect(source).not.toContain("MonitorPage");
  });
});

describe("Sidebar chrome", () => {
  const source = readFileSync(SIDEBAR_PATH, "utf8");

  it("carries no nav-chrome entries for the retired pages", () => {
    // The retired NAV ENTRIES are gone. The 会话 list keeps its
    // <nav aria-label="会话"> landmark — it is real navigation, and
    // removing the landmark would be an accessibility regression.
    expect(source).not.toContain("navChrome");
    expect(source).not.toContain("NAV_ENTRIES");
    expect(source).not.toContain('"/kb"');
    expect(source).not.toContain('"/monitor"');
    // No nav ENTRY labels remain: the retired chrome rendered these as
    // quoted label literals. Prose mentions in comments are fine — the
    // reverted brand subtitle legitimately contains 知识库 too.
    expect(source).not.toContain('"知识库"');
    expect(source).not.toContain('"监控"');
  });

  it("keeps the brand subtitle pinned by ADR 0050", () => {
    // The retire decision covers the nav labels + /kb /monitor routes,
    // not the brand line.
    expect(source).toContain("企业知识库助手");
    expect(source).not.toContain("本地研究工作台");
  });
});

describe("dead corpus plumbing", () => {
  it("App.tsx does not fire the retired boot corpus fetch", () => {
    // The corpus-card chrome is gone, so nothing consumes GET /api/corpus
    // at boot. The store slice stays for the out-of-chrome 入库 path.
    const source = readFileSync(APP_PATH, "utf8");
    expect(source).not.toContain("loadCorpus");
  });

  it("the composer prefill plumbing has no orphaned producer", () => {
    for (const path of [COMPOSER_PATH, THREAD_PAGE_PATH]) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} contains prefill`).not.toContain("prefill");
    }
  });
});

describe("knowledge MCP 409 copy drift guard", () => {
  const host = readFileSync(HOST_KNOWLEDGE_MCP_PATH, "utf8");

  it("keeps the host error token and copy literals", () => {
    expect(host).toContain("知识 MCP 故障");
    expect(host).toContain("knowledge_mcp_fault");
  });

  it("keeps the SPA copy identical to the host message", () => {
    // The left corner renders ``KNOWLEDGE_MCP_FAULT_COPY``; the host
    // answers the same string on its 409. A drift would let the two
    // surfaces name the same fault differently.
    expect(KNOWLEDGE_MCP_FAULT_COPY).toBe("知识 MCP 故障");
  });
});

describe("retired fake-retrieve health copy", () => {
  it("appears nowhere in the non-test product source", () => {
    for (const file of collectSourceFiles(SRC_DIR)) {
      const source = readFileSync(file, "utf8");
      for (const copy of RETIRED_FAKE_HEALTH_COPY) {
        expect(source, `${file} contains ${copy}`).not.toContain(copy);
      }
    }
  });

  it("keeps 「可用」 off the files that render MCP health", () => {
    for (const name of CHROME_HEALTH_FILES) {
      const source = readFileSync(join(SRC_DIR, name), "utf8");
      expect(source, `${name} contains 可用`).not.toContain("可用");
    }
  });
});
