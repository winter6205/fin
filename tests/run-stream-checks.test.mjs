import assert from "node:assert/strict";
import { chmod, mkdtemp, rm, writeFile } from "node:fs/promises";
import { afterEach, test } from "node:test";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

import { default as runStreamChecks } from "../scripts/playwright-stream-checks.mjs";

const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runner = path.join(workspace, "scripts", "run-stream-checks.mjs");
const fakeCliSource = `#!/usr/bin/env node
const args = process.argv.slice(2);
const has = (value) => args.includes(value);

if (has("--version")) {
  console.log("fake-playwright-cli 1.0.0");
  process.exit(0);
}

if (has("open")) {
  console.log(JSON.stringify({ result: {} }));
  process.exit(0);
}

if (has("run-code")) {
  const failedId = process.env.FAKE_FAILED_CHECK_ID;
  const ids = [
    "1-kb-corpus",
    "2-stream-no-retract",
    "4-dom-separation",
    "5-trace-collapse",
    "3-reload-persist",
    "6-status-strip",
  ];
  const checks = ids.map((id) => ({
    id,
    pass: id !== failedId,
    detail: id === failedId ? { injected: true } : {},
  }));
  process.stdout.write(JSON.stringify({
    result: JSON.stringify({ checks, allPass: checks.every((check) => check.pass) }),
  }));
  process.exit(Number(process.env.FAKE_RUN_EXIT ?? 0));
}

if (has("close")) process.exit(0);
process.exit(1);
`;

const tempDirectories = [];

async function runHarness(environment = {}) {
  const directory = await mkdtemp(path.join(tmpdir(), "stream-checks-test-"));
  tempDirectories.push(directory);
  const fakeCli = path.join(directory, "fake-playwright-cli.mjs");
  await writeFile(fakeCli, fakeCliSource, "utf8");
  await chmod(fakeCli, 0o755);

  return spawnSync(process.execPath, [runner, "--base-url=http://127.0.0.1:1"], {
    cwd: workspace,
    encoding: "utf8",
    timeout: 10_000,
    env: {
      ...process.env,
      PLAYWRIGHT_CLI: fakeCli,
      ...environment,
    },
  });
}

afterEach(async () => {
  await Promise.all(
    tempDirectories.splice(0).map((directory) =>
      rm(directory, { recursive: true, force: true }),
    ),
  );
});

test("rejects an all-PASS report when the child exits non-zero", async () => {
  const result = await runHarness({ FAKE_RUN_EXIT: "7" });

  assert.notEqual(
    result.status,
    0,
    `runner succeeded despite child exit 7:\n${result.stdout}\n${result.stderr}`,
  );
});

test("prints a machine-visible FAIL line for a failed check", async () => {
  const result = await runHarness({ FAKE_FAILED_CHECK_ID: "2-stream-no-retract" });

  assert.notEqual(result.status, 0, "failed check must produce a non-success exit");
  assert.match(
    result.stdout,
    /^\[FAIL\]\s+2-stream-no-retract\b/m,
    `missing protocol FAIL line:\n${result.stdout}\n${result.stderr}`,
  );
});

// ---------------------------------------------------------------------------
// A Playwright-shaped `page` double for the composer-arming path.
//
// It models only the nodes the checks address, but it keeps the one
// driver property that broke the lane: a locator that matches more
// than one element throws the same strict-mode violation a real
// driver throws. `evaluate` is deliberately unsupported — checks 4-6
// read a live DOM (and fetch /api/status) and stay the browser
// lane's job.

const el = (tag, props = {}, children = []) => ({
  tag,
  classes: props.classes ?? [],
  attrs: props.attrs ?? {},
  text: props.text ?? "",
  value: props.value ?? "",
  disabled: props.disabled ?? false,
  action: props.action ?? null,
  children,
});

const textOf = (node) => node.text + node.children.map(textOf).join("");

function parseSimpleSelector(token) {
  const simple = { tag: "", classes: [], attrs: [] };
  const rest = token.replace(/\[([^\]=\s]+)="([^"]*)"\]/g, (_all, key, value) => {
    simple.attrs.push([key, value]);
    return "";
  });
  const [tag, ...classes] = rest.split(".");
  simple.tag = tag;
  simple.classes = classes;
  return simple;
}

function matchesSimple(node, simple) {
  if (simple.tag && node.tag !== simple.tag) return false;
  if (!simple.classes.every((name) => node.classes.includes(name))) return false;
  return simple.attrs.every(([key, value]) => node.attrs[key] === value);
}

// Descendant combinator only — no check in the harness queries
// anything richer than `tag`, `.class` and `[attr="value"]`.
function selectFrom(roots, selector) {
  let current = roots;
  for (const token of selector.trim().split(/\s+/)) {
    const simple = parseSimpleSelector(token);
    const found = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matchesSimple(child, simple)) found.push(child);
        walk(child);
      }
    };
    for (const root of current) walk(root);
    current = found;
  }
  return current;
}

function fakeThreadPage() {
  const state = {
    route: "/",
    draft: "",
    tick: 0,
    streaming: false,
    done: false,
    answer: "",
  };

  const actions = {
    "new-thread": () => {
      state.route = "/t/thread-1";
    },
    send: () => {
      state.streaming = true;
      state.done = false;
      state.tick = 0;
      state.answer = "";
      state.draft = "";
    },
  };

  // Rebuilt per query so the answer bubble tracks the stream clock.
  // T1 chrome (specs/workbench-chrome.md): the sidebar is mounted
  // outside <Routes> in App.tsx, so its "+ 新建 thread" button is on
  // every route. The corpus cards moved off "/" onto "/kb", so the
  // "/" main is just the empty/restore placeholder — there is no
  // longer a 12-card grid inline on the home route.
  const sidebarEl = () => [
    el("aside", {}, [
      el("button", { text: "+ 新建 thread", action: "new-thread" }),
    ]),
  ];
  const tree = () => {
    if (state.route === "/") {
      return [...sidebarEl(), el("main", {}, [])];
    }
    if (state.route === "/kb") {
      return [
        ...sidebarEl(),
        el("main", {}, [
          el("div", { attrs: { "data-testid": "ingest-control" } }),
          ...Array.from({ length: 12 }, (_, i) =>
            el("button", { text: `Apple 10-Q ${i} · 24 pages` }),
          ),
        ]),
      ];
    }
    return [
      ...sidebarEl(),
      el("main", {}, [
        // Sidebar watchlist: a SECOND `type="submit"` on the thread
        // page. Rendered first, so the composer is not the first form
        // in document order either.
        el("section", { attrs: { "aria-label": "关注列表" } }, [
          el("form", {}, [
            el("input", { attrs: { "aria-label": "要关注的 issuer" } }),
            el("button", { attrs: { type: "submit" }, text: "+ 关注" }),
          ]),
        ]),
        el("article", {}, [
          el("div", { classes: ["bg-card"] }, [
            el("p", {
              classes: ["text-sm", "whitespace-pre-wrap"],
              text: state.answer,
            }),
          ]),
        ]),
        ...(state.done ? [el("span", { text: "完成" })] : []),
        el("form", {}, [
          el("textarea", { attrs: { placeholder: "输入关于 10-Q 的问题…" } }),
          el("button", {
            attrs: { type: "submit", "data-testid": "composer-send" },
            text: state.streaming ? "运行中…" : "发送",
            disabled: () => state.streaming || state.draft.trim() === "",
            action: "send",
          }),
        ]),
      ]),
    ];
  };

  const locatorFor = (selector, nodes) => {
    const strict = (op) => {
      if (nodes.length > 1) {
        throw new Error(
          `locator.${op}: strict mode violation: locator('${selector}') resolved to ${nodes.length} elements:`,
        );
      }
      if (nodes.length === 0) {
        throw new Error(`locator.${op}: Timeout 30000ms exceeded.`);
      }
      return nodes[0];
    };
    const isDisabled = (node) =>
      typeof node.disabled === "function" ? node.disabled() : node.disabled === true;

    return {
      async count() {
        return nodes.length;
      },
      first() {
        return locatorFor(`${selector} >> nth=0`, nodes.slice(0, 1));
      },
      last() {
        return locatorFor(`${selector} >> nth=-1`, nodes.slice(-1));
      },
      locator(sub) {
        return locatorFor(`${selector} ${sub}`, selectFrom(nodes, sub));
      },
      filter(options = {}) {
        const wanted = options.hasText ?? "";
        const keep = nodes.filter((node) =>
          wanted instanceof RegExp
            ? wanted.test(textOf(node))
            : textOf(node).includes(String(wanted)),
        );
        return locatorFor(`${selector}:has-text(${wanted})`, keep);
      },
      async isEnabled() {
        return !isDisabled(strict("isEnabled"));
      },
      async isVisible() {
        strict("isVisible");
        return true;
      },
      async fill(value) {
        const node = strict("fill");
        if (node.tag === "textarea") state.draft = value;
        else node.value = value;
      },
      async inputValue() {
        const node = strict("inputValue");
        return node.tag === "textarea" ? state.draft : node.value;
      },
      async click() {
        const node = strict("click");
        if (node.action) actions[node.action]();
      },
      async textContent() {
        return textOf(strict("textContent"));
      },
      async waitFor() {
        strict("waitFor");
      },
      async evaluate() {
        strict("evaluate");
        throw new Error(
          "fake page: evaluate is not modelled — checks 4-6 belong to the browser lane",
        );
      },
    };
  };

  return {
    state,
    async goto(url) {
      state.route = new URL(url).pathname;
    },
    async reload() {
      /* turns are server state; the route and the bubble stand */
    },
    async waitForURL(pattern) {
      if (!pattern.test(state.route)) {
        throw new Error("page.waitForURL: Timeout 10000ms exceeded.");
      }
    },
    async waitForTimeout() {
      await new Promise((resolve) => setTimeout(resolve, 0));
      if (!state.streaming) return;
      state.tick += 1;
      state.answer = "x".repeat(10 * Math.min(state.tick, 4));
      if (state.tick >= 4) {
        state.streaming = false;
        state.done = true;
      }
    },
    async waitForSelector(selector) {
      if (selectFrom(tree(), selector).length === 0) {
        throw new Error("page.waitForSelector: Timeout 10000ms exceeded.");
      }
    },
    locator(selector) {
      return locatorFor(selector, selectFrom(tree(), selector));
    },
    // Only `button` is ever queried by role, so the role maps to the
    // tag name; `name` matches as a case-insensitive substring, as
    // Playwright does by default.
    getByRole(role, options = {}) {
      const name = options.name ?? "";
      const nodes = selectFrom(tree(), role).filter((node) =>
        textOf(node).toLowerCase().includes(name.toLowerCase()),
      );
      return locatorFor(`role=${role}[name=${name}]`, nodes);
    },
    getByText(text) {
      const all = [];
      const walk = (node) => {
        all.push(node);
        node.children.forEach(walk);
      };
      tree().forEach(walk);
      return locatorFor(
        `text=${text}`,
        all.filter((node) => textOf(node).includes(text)),
      );
    },
    async evaluate() {
      throw new Error(
        "fake page: evaluate is not modelled — checks 4-6 belong to the browser lane",
      );
    },
  };
}

// The thread page renders the composer's 发送 AND the watchlist's
// + 关注, both `type="submit"`. Addressing submit buttons page-wide
// therefore resolves to two elements and every call on the locator
// dies in strict mode — which is how the lane lost checks 2/4/5
// while the product itself was fine.
test("arms the composer on a thread page that also renders the watchlist submit", async () => {
  const page = fakeThreadPage();
  const report = await runStreamChecks(page, {
    baseUrl: "http://127.0.0.1:1",
    pollIntervalMs: 0,
    pollCount: 40,
    requireStreamGrowth: true,
  });

  const stream = report.checks.find((check) => check.id === "2-stream-no-retract");
  assert.ok(stream, `2-stream-no-retract produced no verdict: ${JSON.stringify(report)}`);
  assert.equal(
    stream.pass,
    true,
    `2-stream-no-retract must pass on a page with two submit buttons; got ` +
      `${stream.error ?? JSON.stringify(stream.detail)}`,
  );
});

test("never addresses submit buttons page-wide", () => {
  // Comments say WHY the page-wide form is wrong; only the shipped
  // code can regress it, so the prose is dropped before matching.
  const body = runStreamChecks.toString().replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(
    body,
    /button\[type="submit"\]/,
    "the send button must be scoped to the composer, not to every form on the page",
  );
});
