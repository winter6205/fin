import assert from "node:assert/strict";
import { createServer } from "node:http";
import { chmod, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { afterEach, test } from "node:test";
import { tmpdir } from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { startScriptedSseProxy } from "../scripts/lib/scripted-sse-proxy.mjs";

import {
  declaredCheckIds,
  default as runStreamChecks,
} from "../scripts/playwright-stream-checks.mjs";
import * as liveSmoke from "../scripts/run-live-smoke.mjs";

const workspace = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const runner = path.join(workspace, "scripts", "run-stream-checks.mjs");
const scriptedRunner = path.join(workspace, "scripts", "run-scripted-e2e.mjs");
const fakeCliSource = `#!/usr/bin/env node
const args = process.argv.slice(2);
const has = (value) => args.includes(value);

if (has("--version")) {
  console.log("fake-playwright-cli 1.0.0");
  process.exit(0);
}

if (process.env.FAKE_RAW_ONLY && has("--json")) process.exit(2);

if (has("open")) {
  console.log(JSON.stringify({ result: {} }));
  process.exit(0);
}

if (has("run-code")) {
  const failedId = process.env.FAKE_FAILED_CHECK_ID;
  const ids = [
    "1-home-chrome",
    "2-stream-no-retract",
    "3-reload-persist",
    "4-dom-separation",
    "5-trace-collapse",
    "6-boot-status",
    "7-stream-scroll-follow",
    "8-scroll-follow-lifecycle",
  ];
  const checks = ids.map((id) => ({
    id,
    pass: id !== failedId,
    detail: id === failedId ? { injected: true } : {},
  }));
  const report = { checks, allPass: checks.every((check) => check.pass) };
  process.stdout.write(JSON.stringify(has("--raw") ? report : { result: JSON.stringify(report) }));
  process.exit(Number(process.env.FAKE_RUN_EXIT ?? 0));
}

if (has("close")) process.exit(0);
process.exit(1);
`;

const tempDirectories = [];

async function listen(server) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  return `http://127.0.0.1:${address.port}`;
}

async function closeServer(server) {
  if (!server.listening) return;
  await new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
    server.closeAllConnections();
  });
}

test("scripted SSE proxy waits for completion and paces only nonempty answers", async () => {
  const answerGapMs = 120;
  const frames = [
    'event: thinking\ndata: {"text":"thinking"}',
    'event: answer\ndata: {"text":"safe first piece"}',
    'event: tool_result\ndata: {"name":"retrieve"}',
    'event: answer\ndata: {"text":"safe second piece"}',
    'event: answer\ndata: {"text":""}',
    "event: citations\ndata: []",
  ];
  const expected = Buffer.from(frames.map((frame) => `${frame}\n\n`).join(""));
  let upstreamCompletedAt = 0;
  const upstream = createServer(async (_request, response) => {
    response.writeHead(200, {
      "content-type": "text/event-stream; charset=utf-8",
      "x-upstream-frame": "preserved",
    });
    response.write(expected.subarray(0, 17));
    await new Promise((resolve) => setTimeout(resolve, 25));
    response.end(expected.subarray(17));
    upstreamCompletedAt = Date.now();
  });
  const upstreamBaseUrl = await listen(upstream);
  const proxy = await startScriptedSseProxy({
    upstreamBaseUrl,
    answerFrameDelayMs: answerGapMs,
  });

  try {
    const response = await fetch(`${proxy.baseUrl}/api/stream`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: "{}",
    });
    const received = [];
    let joined = Buffer.alloc(0);
    let firstAnswerAt = 0;
    let secondAnswerAt = 0;
    let emptyAnswerAt = 0;
    for await (const chunk of response.body) {
      const bytes = Buffer.from(chunk);
      received.push(bytes);
      joined = Buffer.concat([joined, bytes]);
      if (!firstAnswerAt && joined.includes(Buffer.from("safe first piece"))) {
        firstAnswerAt = Date.now();
      }
      if (!secondAnswerAt && joined.includes(Buffer.from("safe second piece"))) {
        secondAnswerAt = Date.now();
      }
      if (!emptyAnswerAt && joined.includes(Buffer.from('data: {"text":""}'))) {
        emptyAnswerAt = Date.now();
      }
    }

    assert.equal(response.status, 200);
    assert.equal(response.headers.get("x-upstream-frame"), "preserved");
    assert.equal(response.headers.get("content-length"), String(expected.length));
    assert.deepEqual(joined, expected);
    assert.ok(firstAnswerAt >= upstreamCompletedAt, "proxy must buffer to upstream end");
    assert.ok(secondAnswerAt - firstAnswerAt >= answerGapMs - 20);
    assert.ok(emptyAnswerAt - secondAnswerAt < answerGapMs - 20);
    assert.ok(received.length >= 2, "answer frames must remain separately consumable");
  } finally {
    await proxy.close();
    await closeServer(upstream);
  }
});

test("scripted SSE proxy rejects empty, invalid, remote, and overflowing inputs", async () => {
  for (const upstreamBaseUrl of [undefined, "", "not a URL", "file:///tmp/test"]) {
    await assert.rejects(
      startScriptedSseProxy({ upstreamBaseUrl }),
      TypeError,
    );
  }
  for (const upstreamBaseUrl of [
    "http://example.com",
    "http://user:password@127.0.0.1:8001",
    "http://127.0.0.1:8001/prefix",
  ]) {
    await assert.rejects(
      startScriptedSseProxy({ upstreamBaseUrl }),
      TypeError,
    );
  }
  for (const answerFrameDelayMs of [-1, 0.5, Number.NaN, Number.MAX_SAFE_INTEGER, null]) {
    await assert.rejects(
      startScriptedSseProxy({
        upstreamBaseUrl: "http://127.0.0.1:8001",
        answerFrameDelayMs,
      }),
      RangeError,
    );
  }
});

test("scripted SSE proxy streams non-SSE responses without its answer pacing", async () => {
  const body = Buffer.from('{"message":"part one and part two"}');
  let upstreamCompleted = false;
  const upstream = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.write(body.subarray(0, 16));
    setTimeout(() => {
      upstreamCompleted = true;
      response.end(body.subarray(16));
    }, 80);
  });
  const upstreamBaseUrl = await listen(upstream);
  const proxy = await startScriptedSseProxy({
    upstreamBaseUrl,
    answerFrameDelayMs: 500,
  });

  try {
    const response = await fetch(`${proxy.baseUrl}/api/status`);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.equal(upstreamCompleted, false, "non-SSE body must remain live");
    const rest = [];
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      rest.push(Buffer.from(next.value));
    }
    assert.deepEqual(
      Buffer.concat([Buffer.from(first.value), ...rest]),
      body,
    );
  } finally {
    await proxy.close();
    await closeServer(upstream);
  }
});

test("scripted SSE proxy keeps concurrent answer pacing request-local", async () => {
  const upstream = createServer((request, response) => {
    const label = new URL(request.url, "http://127.0.0.1").searchParams.get("id");
    const body = Buffer.from(
      `event: answer\ndata: {"text":"${label}-one"}\n\n` +
        `event: answer\ndata: {"text":"${label}-two"}\n\n`,
    );
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end(body);
  });
  const upstreamBaseUrl = await listen(upstream);
  const proxy = await startScriptedSseProxy({
    upstreamBaseUrl,
    answerFrameDelayMs: 80,
  });

  try {
    const responses = await Promise.all(["first", "second"].map(async (id) => {
      const response = await fetch(`${proxy.baseUrl}/api/stream?id=${id}`);
      return Buffer.from(await response.arrayBuffer());
    }));
    assert.match(responses[0].toString(), /first-one[\s\S]*first-two/);
    assert.match(responses[1].toString(), /second-one[\s\S]*second-two/);
  } finally {
    await proxy.close();
    await closeServer(upstream);
  }
});

test("scripted SSE proxy reports upstream connection failures without leaking details", async () => {
  const upstream = createServer();
  const unavailableBaseUrl = await listen(upstream);
  await closeServer(upstream);
  const proxy = await startScriptedSseProxy({ upstreamBaseUrl: unavailableBaseUrl });

  try {
    const response = await fetch(`${proxy.baseUrl}/api/status`);
    const body = await response.text();
    assert.equal(response.status, 502);
    assert.equal(body, "scripted SSE proxy upstream unavailable");
    assert.doesNotMatch(body, /ECONNREFUSED|127\.0\.0\.1/);
  } finally {
    await proxy.close();
  }
});

test("scripted SSE proxy closes promptly when the browser cancels during a paced gap", async () => {
  const upstream = createServer((_request, response) => {
    response.writeHead(200, { "content-type": "text/event-stream" });
    response.end(
      'event: answer\ndata: {"text":"safe one"}\n\n' +
        'event: answer\ndata: {"text":"safe two"}\n\n',
    );
  });
  const upstreamBaseUrl = await listen(upstream);
  const proxy = await startScriptedSseProxy({
    upstreamBaseUrl,
    answerFrameDelayMs: 1_000,
  });

  try {
    const response = await fetch(`${proxy.baseUrl}/api/stream`);
    const reader = response.body.getReader();
    const first = await reader.read();
    assert.ok(first.value);
    await reader.cancel();
    const started = Date.now();
    await proxy.close();
    assert.ok(Date.now() - started < 500, "close must cancel the outstanding timer");
  } finally {
    await closeServer(upstream);
  }
});

test("only the scripted browser lane routes through the completed-response proxy", async () => {
  const [scriptedSource, generalSource] = await Promise.all([
    readFile(scriptedRunner, "utf8"),
    readFile(runner, "utf8"),
  ]);
  assert.match(scriptedSource, /startScriptedSseProxy/);
  assert.match(scriptedSource, /upstreamBaseUrl: baseUrl/);
  assert.match(scriptedSource, /`--base-url=\$\{sseProxy\.baseUrl\}`/);
  assert.match(scriptedSource, /SCRIPTED_MODEL_IDENTITY = "scripted-test-model"/);
  assert.match(scriptedSource, /env\.FIN_MODEL = SCRIPTED_MODEL_IDENTITY/);
  const proxyCloseIndex = scriptedSource.indexOf("await sseProxy.close()");
  const workspaceStopIndex = scriptedSource.indexOf("await stopWorkspace(child)");
  assert.ok(proxyCloseIndex >= 0);
  assert.ok(
    workspaceStopIndex > proxyCloseIndex,
    "the proxy must be closed before the scripted workspace cleanup",
  );
  assert.doesNotMatch(generalSource, /startScriptedSseProxy/);
});

test("live smoke resolves model identity only from an override or projected settings", () => {
  const { resolveConfiguredModel } = liveSmoke;
  assert.equal(typeof resolveConfiguredModel, "function");
  assert.equal(
    resolveConfiguredModel({
      override: "  explicit-model  ",
      env: { FIN_MODEL: "environment-model" },
      settings: { FIN_MODEL: "settings-model" },
    }),
    "explicit-model",
  );
  assert.equal(
    resolveConfiguredModel({
      env: { FIN_MODEL: " " },
      settings: { FIN_MODEL: " configured-model " },
    }),
    "configured-model",
  );
  assert.equal(
    resolveConfiguredModel({ env: { FIN_MODEL: " " }, settings: {} }),
    "",
  );
});

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

test("requires scroll-follow coverage only when streamed growth is required", () => {
  assert.equal(declaredCheckIds().includes("7-stream-scroll-follow"), false);
  assert.equal(
    declaredCheckIds({ requireStreamGrowth: true }).includes(
      "7-stream-scroll-follow",
    ),
    true,
  );
  assert.equal(
    declaredCheckIds({ requireStreamGrowth: true }).includes(
      "8-scroll-follow-lifecycle",
    ),
    true,
  );
});

// The document-choice lane pauses its first send instead of answering it,
// so the answered-turn checks (2/4/5/3) and the scroll-follow pair have
// nothing to read there. This pins BOTH directions, because the risk in
// that scoping is silence: a change that dropped these ids everywhere
// would still pass a lane that runs them, and one that kept them would
// report the pause permanently red.
//
// The default and bounce lanes must keep declaring the FULL set — that is
// what makes the scoping a per-lane decision rather than a deletion.
const ANSWERED_TURN_IDS = [
  "2-stream-no-retract",
  "4-dom-separation",
  "5-trace-collapse",
  "3-reload-persist",
];
const CHROME_IDS = ["1-home-chrome", "6-boot-status"];

test("every non-document-choice lane still declares the full check set", () => {
  for (const options of [
    {},
    { requireStreamGrowth: true },
    { expectBounce: true },
    { requireStreamGrowth: true, expectBounce: true },
  ]) {
    const ids = declaredCheckIds(options);
    for (const id of [...CHROME_IDS, ...ANSWERED_TURN_IDS]) {
      assert.ok(
        ids.includes(id),
        `${id} must stay declared for ${JSON.stringify(options)}; got ${ids.join(", ")}`,
      );
    }
    // The scroll-follow pair is opt-in on streamed growth, exactly as it
    // was before this change — only the document-choice lane drops it.
    const wantScroll = options.requireStreamGrowth === true;
    assert.equal(ids.includes("7-stream-scroll-follow"), wantScroll);
    assert.equal(ids.includes("8-scroll-follow-lifecycle"), wantScroll);
    // A lane that was not asked for the bounce / document-choice checks
    // must not report them: a check that always runs would be a FAIL on
    // a workspace that cannot produce the condition it drives.
    assert.equal(ids.includes("7-bounce-no-blank"), options.expectBounce === true);
    assert.equal(
      ids.includes("7-document-choice-pick"),
      options.expectDocumentChoice === true,
    );
  }
});

test("the document-choice lane declares the choice check instead of an answered turn", () => {
  const ids = declaredCheckIds({
    expectDocumentChoice: true,
    requireStreamGrowth: true,
  });
  // The chrome is route-scoped and holds for every scenario.
  for (const id of CHROME_IDS) {
    assert.ok(ids.includes(id), `${id} must stay declared on the choice lane`);
  }
  assert.ok(
    ids.includes("7-document-choice-pick"),
    "the document-choice lane must declare its own check",
  );
  // The answered-turn checks read a turn that streamed to DONE, which
  // this transcript never produces on its first send.
  for (const id of ANSWERED_TURN_IDS) {
    assert.equal(
      ids.includes(id),
      false,
      `${id} reads a completed turn and must not be declared on a lane whose first send pauses`,
    );
  }
  assert.equal(ids.includes("7-stream-scroll-follow"), false);
  assert.equal(ids.includes("8-scroll-follow-lifecycle"), false);
  assert.equal(ids.includes("7-bounce-no-blank"), false);
});

// The check bodies are gated on the same flag the declaration is, so a
// declared-but-unreported id cannot become a permanent FAIL. These assert
// the GATE exists in the shipped source: a lane whose transcript pauses
// while the gate was dropped would run four checks that can never pass.
test("the answered-turn checks are gated on the same flag they are declared with", () => {
  const body = runStreamChecks.toString().replace(/^\s*\/\/.*$/gm, "");
  assert.match(
    body,
    /const answeredTurnChecks = !cfg\.expectDocumentChoice/,
    "the answered-turn gate must exist",
  );
  assert.match(
    body,
    /if \(answeredTurnChecks\) \{\s*await check\("2-stream-no-retract"/,
    "check 2 must be inside the answered-turn gate",
  );
  assert.match(
    body,
    /if \(answeredTurnChecks\) \{\s*await check\("3-reload-persist"/,
    "check 3 must be inside the answered-turn gate",
  );
  assert.match(
    body,
    /if \(cfg\.requireStreamGrowth && answeredTurnChecks\)/,
    "the scroll-follow block must be gated on the answered-turn flag too",
  );
});

// The document-choice check is registered under the option name the lane
// passes, and its id is spelled as a literal inside the function body —
// the function travels to the browser driver as SOURCE TEXT, so a
// module-scope constant referenced there would be a ReferenceError at run
// time rather than a compile error.
test("the serialized document-choice helper is gated, named, and executable", async () => {
  const body = runStreamChecks.toString().replace(/^\s*\/\/.*$/gm, "");
  assert.match(
    body,
    /const runDocumentChoiceWorkflow = async \(\) => \{/,
    "the workflow helper must be defined inside the serialized runStreamChecks function",
  );
  assert.match(
    body,
    /const maybeRunDocumentChoiceCheck = async \(\) => \{\s*if \(!cfg\.expectDocumentChoice\) return;\s*await check\("7-document-choice-pick", runDocumentChoiceWorkflow\);\s*\};\s*await maybeRunDocumentChoiceCheck\(\);/,
    "the literal check id and workflow helper must be registered only when requested",
  );
  // No module-scope id constant may be referenced inside the body: the
  // snippet is evaluated on its own in the driver process.
  assert.doesNotMatch(
    body,
    /DOCUMENT_CHOICE_CHECK_ID|SCROLL_CHECK_ID|CHROME_CHECK_IDS|ANSWER_TURN_CHECK_IDS|BASE_CHECK_IDS/,
  );

  // The browser runner evaluates this function's source in a fresh driver
  // process. Reconstruct that serialized function and execute its opt-in
  // branch against the page double: reaching the document-choice card wait
  // proves the named helper was invoked, not merely present in the source.
  const serializedRunStreamChecks = new Function(`return (${body});`)();
  const fixtureOptions = {
    baseUrl: "http://127.0.0.1:1",
    navTimeoutMs: 1_000,
    pollIntervalMs: 0,
    pollCount: 1,
  };
  const disabledReport = await serializedRunStreamChecks(fakeThreadPage(), {
    ...fixtureOptions,
    expectDocumentChoice: false,
  });
  assert.equal(
    disabledReport.checks.some((check) => check.id === "7-document-choice-pick"),
    false,
    "the serialized helper must remain absent when its option is disabled",
  );
  const report = await serializedRunStreamChecks(fakeThreadPage(), {
    ...fixtureOptions,
    expectDocumentChoice: true,
  });
  const documentChoice = report.checks.find(
    (check) => check.id === "7-document-choice-pick",
  );
  assert.ok(documentChoice, "the opted-in helper must produce its declared check");
  assert.match(
    documentChoice.error,
    /await document choice card/,
    "the serialized helper must execute its browser workflow",
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
  // T2 chrome (specs/workbench-chrome.md): the watchlist CRUD + notice
  // dismissal moved off the sidebar onto /monitor — the thread page
  // (and its sidebar) no longer render the watchlist form, so the
  // composer is the only `type="submit"` left on this route.
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
    if (state.route === "/monitor") {
      // T2: the watchlist surface lives on /monitor. T3: the boot-
      // scoped status panel + 重连 control live here too. The mirror
      // renders just enough of both to keep the tree realistic for
      // the lane; the composer is NOT on this route so the submit
      // count is 1 (composer-arming check stays scoped).
      return [
        ...sidebarEl(),
        el("main", {}, [
          el("section", {
            attrs: { "data-testid": "monitor-status-panel" },
          }, [
            el("p", {
              attrs: { "data-testid": "monitor-status-headline" },
              text: "检索：可用",
            }),
            el("p", {
              attrs: { "data-testid": "monitor-status-detail" },
              text: "已连接 iknow-memory 检索服务；语料 12 份。",
            }),
            el("button", {
              attrs: { type: "button", "data-testid": "monitor-reconnect" },
              text: "重连",
            }),
          ]),
          el("section", { attrs: { "aria-label": "关注列表" } }, [
            el("form", {}, [
              el("input", { attrs: { "aria-label": "要关注 issuer" } }),
              el("button", { attrs: { type: "submit" }, text: "+ 关注" }),
            ]),
          ]),
        ]),
      ];
    }
    return [
      ...sidebarEl(),
      el("main", {}, [
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
    url() {
      return new URL(state.route, "http://127.0.0.1:1").toString();
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

// T2 chrome (specs/workbench-chrome.md): the watchlist + its
// `+ 关注` submit moved off the thread page onto /monitor, so the
// thread page now renders only the composer. The original lane
// regressed because a page-wide submit locator resolved to two
// elements (composer + watchlist) and died in strict mode. Today
// the watchlist submit lives at /monitor and the thread page has
// exactly one submit; the composer-only assertion still guards
// that the harness scopes the locator it means to scope.
test("arms the composer on a thread page whose only submit is the composer", async () => {
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
    `2-stream-no-retract must pass on a thread page with a single submit; got ` +
      `${stream.error ?? JSON.stringify(stream.detail)}`,
  );

  const lifecycle = report.checks.find(
    (check) => check.id === "8-scroll-follow-lifecycle",
  );
  assert.ok(
    lifecycle,
    "the required lifecycle check must produce a verdict for the test page",
  );
  assert.equal(
    lifecycle.pass,
    false,
    "an incomplete page double must fail explicitly instead of omitting lifecycle coverage",
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

test("scroll lifecycle distinguishes a completed backend run from an open browser SSE reader", () => {
  const body = runStreamChecks.toString();
  assert.match(
    body,
    /Object\.defineProperty\(body, "getReader"/,
    "the lifecycle check must observe the real response reader used by the client",
  );
  assert.match(body, /backendCompletedBeforeReplay/);
  assert.match(body, /browserStreamOpenAtResize/);
  assert.match(body, /browserStreamSettledAfterNavigation/);
  assert.doesNotMatch(
    body,
    /pendingAtResize:.*readThreadInFlight/s,
    "backend run leases cannot stand in for the browser's still-open SSE response",
  );
});

test("bounce browser coverage proves the hidden draft stays private while the retry answer lands", () => {
  const body = runStreamChecks.toString();
  assert.match(body, /leakedUngroundedDraft/);
  assert.match(body, /admittedRetryMarker/);
  assert.match(body, /sourceReadObserved/);
  assert.doesNotMatch(
    body,
    /pass:\s*drafted\s*&&/,
    "the bounce lane must not require an unadmitted draft to appear in the browser",
  );
});

// T2 header-only collapse: collapsed thinking has toggles + no full
// burst; expand shows one full. A tail-preview node is not part of
// the pass contract (specs/turn-trace-folds.md).
test("5-trace-collapse does not require thinking tail preview nodes", () => {
  const body = runStreamChecks.toString().replace(/^\s*\/\/.*$/gm, "");
  assert.doesNotMatch(
    body,
    /collapsed\.previews\s*===\s*collapsed\.thinkingToggles/,
    "header-only collapse must not require one preview node per thinking toggle",
  );
  assert.match(body, /collapsed\.fulls\s*===\s*0/);
  assert.match(body, /expanded\.fulls\s*===\s*1/);
});

test("uses the supported raw CLI protocol", async () => {
  const result = await runHarness({ FAKE_RAW_ONLY: "1" });
  assert.equal(result.status, 0, `${result.stdout}\n${result.stderr}`);
});
