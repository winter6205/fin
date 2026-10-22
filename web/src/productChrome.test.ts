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
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { KNOWLEDGE_MCP_FAULT_COPY } from "./knowledgeMcp";
import { PRODUCT_ROUTE_PATHS } from "./productRoutes";
import { decideThreadEntryRoute } from "./App";
import Sidebar from "./components/Sidebar";
import { DemoHomeLink } from "./pages/ThreadPage";

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

/** The JSX returned by the default export, comments stripped.
 *
 * Source-level guards must not fire on the comments that explain WHY
 * the code is correct — the docstrings here legitimately name the
 * concepts the rendered chrome must not carry. */
function jsxBody(source: string): string {
  const start = source.indexOf("return (");
  return (start === -1 ? source : source.slice(start)).replace(
    /\/\*[\s\S]*?\*\/|\/\/[^\n]*/g,
    "",
  );
}

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

  it("renders the T4 bound 资料集合 line from the shared helper", () => {
    // SC8-style source guard (this file's existing pattern): the line
    // must come from ``boundCollectionLine`` so the copy and the
    // 未绑定 / bound split stay unit-tested in ``knowledgeMcp.ts``
    // rather than hardcoded in the component.
    expect(source).toContain("boundCollectionLine");
    expect(source).toContain('data-testid="bound-collection-line"');
    // The 就绪 word is pinned nowhere in the chrome's COPY — the
    // source's own comments explain that the line makes no readiness
    // claim (spec assumption 9: readiness is iknow's own 资料页), so
    // the guard is over the JSX body, not the file text.
    expect(source).not.toContain(">就绪");
    expect(source).not.toContain("就绪<");
    expect(jsxBody(source)).not.toContain("就绪");
    expect(jsxBody(source)).not.toContain("corpus_filing_count");
  });
});

describe("dead corpus plumbing", () => {
  it("App.tsx does not fire the retired boot corpus fetch", () => {
    // The corpus-card chrome is gone, so nothing consumes GET /api/corpus
    // at boot. The store slice stays for the out-of-chrome 入库 path, and
    // the T6 generic overview is loaded on demand by its own home surface
    // rather than here.
    const source = readFileSync(APP_PATH, "utf8");
    expect(source).not.toContain("loadCorpus");
    expect(source).not.toContain("loadCatalogOverview");
  });

  it("the composer prefill plumbing has no orphaned producer", () => {
    for (const path of [COMPOSER_PATH, THREAD_PAGE_PATH]) {
      const source = readFileSync(path, "utf8");
      expect(source, `${path} contains prefill`).not.toContain("prefill");
    }
  });

  it("the retired corpus-card chrome does not return on the boot path", () => {
    // T6 added a catalog OVERVIEW to home. That is the ingest area's own
    // surface, not the retired thread-page corpus wall: it must not reach
    // App's boot effects or the thread page, or the seam this spec
    // retired comes back through a different door.
    const app = readFileSync(APP_PATH, "utf8");
    expect(app).not.toContain("getCatalogOverview");
    expect(app).not.toContain("catalogOverview");
    expect(readFileSync(THREAD_PAGE_PATH, "utf8")).not.toContain(
      "catalogOverview",
    );
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

// ----- T3: the navigation contract (plans/2026-10-20-qa-design-correction
// task 3) and T2's route half (task 2). -----
//
// The chrome is rendered with ``renderToString`` because this project's
// vitest setup has no DOM testing library (same split as
// VisualClaims.test.ts / ArchiveList.test.ts). The nav contract is a
// RENDERED fact, not a source string: the accessible name of a link is
// its text, so the assertions read the rendered anchor.

const THREAD_ID = "11111111-1111-4111-8111-111111111111";

/** The rendered anchors whose href is exactly ``href``. */
function anchorsTo(html: string, href: string): string[] {
  return (
    html.match(new RegExp(`<a [^>]*href="${href}"[^>]*>[\\s\\S]*?</a>`, "g")) ?? []
  );
}

/** Visible text of rendered markup: tags and React's comment markers gone. */
function visibleText(html: string): string {
  return html
    .replace(/<!--[\s\S]*?-->/g, "")
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function renderSidebar(): string {
  return renderToString(
    createElement(
      MemoryRouter,
      { initialEntries: ["/"] },
      createElement(Sidebar),
    ),
  );
}

function renderDemoHomeLink(enabled: boolean): string {
  return renderToString(
    createElement(
      MemoryRouter,
      { initialEntries: ["/"] },
      createElement(DemoHomeLink, { enabled }),
    ),
  );
}

describe("the sidebar's home link", () => {
  it("is named exactly 首页 and points at the homepage", () => {
    const links = anchorsTo(renderSidebar(), "/");
    expect(links).toHaveLength(1);
    expect(visibleText(links[0])).toBe("首页");
  });

  it("keeps the 会话 navigation landmark and the thread list", () => {
    const html = renderSidebar();
    expect(html).toContain('aria-label="会话"');
    expect(html).toContain("会话");
  });
});

describe("the demo return link", () => {
  it("is named exactly 返回演示首页 and points at the homepage", () => {
    const links = anchorsTo(renderDemoHomeLink(true), "/");
    expect(links).toHaveLength(1);
    expect(visibleText(links[0])).toBe("返回演示首页");
  });

  it("is absent outside demo mode", () => {
    // The demo link is decided by the demo presentation the ancestor
    // provider carries, so a non-demo conversation shows no demo door.
    expect(renderDemoHomeLink(false)).toBe("");
  });

  it("is decided by the demo presentation the route provides", () => {
    // The thread page does not ask the server again and does not
    // guess: it reads the demo fact its ancestor provider carries.
    const source = readFileSync(THREAD_PAGE_PATH, "utf8");
    expect(source).toContain("useDemoPresentation");
    expect(source).toMatch(/<DemoHomeLink enabled=\{demo\.enabled\}/);
  });
});

describe("the thread route's entry decision", () => {
  const facts = {
    threadId: THREAD_ID,
    sendingThreadId: null,
    activeThreadId: THREAD_ID,
    reading: false,
    fault: null,
  };

  it("keeps the live answer of a thread that is already streaming", () => {
    // T2: entering a thread that is already answering on this client
    // must not paint the entry loader over the live answer. The pane
    // still holds that thread, so the live state is on screen.
    const decision = decideThreadEntryRoute({
      ...facts,
      sendingThreadId: THREAD_ID,
      reading: true,
    });
    expect(decision.view).toBe("live");
  });

  it("reads a thread again when the operator left it while it was streaming", () => {
    // The send is still running, but the pane holds another thread:
    // the live state is gone, so the entry read is REQUIRED and the
    // route shows its loader while that read runs. The store's guard
    // skips a read only when the pane still holds the thread.
    const decision = decideThreadEntryRoute({
      ...facts,
      activeThreadId: "22222222-2222-4222-8222-222222222222",
      sendingThreadId: THREAD_ID,
      reading: true,
    });
    expect(decision.view).toBe("loading");
  });

  it("does not let a failed entry read clear the live turn table", () => {
    const decision = decideThreadEntryRoute({
      ...facts,
      sendingThreadId: THREAD_ID,
      fault: "missing",
    });
    expect(decision.mayClearActiveThread).toBe(false);
  });

  it("shows the loader while an ordinary entry read is in flight", () => {
    expect(decideThreadEntryRoute({ ...facts, reading: true }).view).toBe(
      "loading",
    );
  });

  it("keeps the three refusal distinctions apart", () => {
    expect(decideThreadEntryRoute({ ...facts, fault: "gated" }).view).toBe(
      "gated",
    );
    expect(
      decideThreadEntryRoute({ ...facts, fault: "pending_unavailable" }).view,
    ).toBe("pending_unavailable");
    // A missing thread is the one the pane does NOT hold: the refusal
    // and the empty pane say the same thing.
    expect(
      decideThreadEntryRoute({
        ...facts,
        activeThreadId: null,
        fault: "missing",
      }).view,
    ).toBe("missing");
  });

  it("renders the thread once it is loaded", () => {
    expect(decideThreadEntryRoute(facts).view).toBe("thread");
  });

  it("may clear the pane only for its own thread, and never while it streams", () => {
    expect(
      decideThreadEntryRoute({ ...facts, activeThreadId: THREAD_ID })
        .mayClearActiveThread,
    ).toBe(true);
    // Another thread's pane is not this route's to clear.
    expect(
      decideThreadEntryRoute({ ...facts, activeThreadId: "other-thread" })
        .mayClearActiveThread,
    ).toBe(false);
    // A streaming thread keeps its turn table.
    expect(
      decideThreadEntryRoute({
        ...facts,
        sendingThreadId: THREAD_ID,
      }).mayClearActiveThread,
    ).toBe(false);
  });
});

describe("the startup owner lives at the application lifetime", () => {
  const app = readFileSync(APP_PATH, "utf8");
  const home = readFileSync(join(SRC_DIR, "pages", "HomePage.tsx"), "utf8");

  it("is the application root, not the homepage mount", () => {
    // The homepage mount dies with the route; the opportunity must not.
    // The RULES live in the pure reducer — they are covered
    // behaviourally in ``lastThread.test.ts`` — and the application root
    // only drives it. This source check says where the owner sits, not
    // what it decides.
    expect(app).toContain("useStartupRestore");
    expect(app).toContain("reduceStartupOwner");
    expect(home).not.toContain("decideLastThreadRestore");
    expect(home).not.toContain("restoreAttempted");
  });

  it("never persists the consumed bit", () => {
    // A reload must grant a fresh startup opportunity, so the consumed
    // bit may not live in browser storage.
    expect(app).not.toContain("localStorage.setItem");
    expect(app).not.toContain("sessionStorage");
  });

  it("keeps the homepage in the history when an example is opened", () => {
    // The example navigation is an explicit push: the homepage stays a
    // history entry, so Back returns to it.
    expect(home).toContain("replace: false");
    expect(home).not.toContain("replace: true");
  });

  it("reads the thread-scoped in-flight fact on the route", () => {
    expect(app).toContain("sendingThreadId");
  });
});

