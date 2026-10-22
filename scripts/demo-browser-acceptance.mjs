#!/usr/bin/env node
// Real-browser acceptance for the ZEUS demo: visible demo chrome, grounded
// answers and citations, refusal, same-thread follow-up, clarification,
// developer-only receipt, the demo reset control, and the corrected routing
// contract (ADR 0104 / spec search-mode-host):
//
//   - the rendered UI never names an internal retrieval mode or shows a
//     retrieval-mode picker, at ANY point of the run;
//   - the developer receipt shows decision provenance (tier + confidence +
//     exit) and no mode token;
//   - the answer is visible while the send is still in flight and entering a
//     thread paints no entry loading overlay;
//   - the demo home is reachable from a conversation, from the sidebar, and
//     with the browser Back button, while a send is in flight or not;
//   - no request ever carries a `tiers` field.
//
//   bash scripts/demo-browser-acceptance.sh
//
// The trusted launcher verifies all backing PostgreSQL identities and sets
// FIN_DEMO_ACCEPTANCE_READY before invoking this runner. This file also checks
// the local demo endpoint before opening a browser. It never mocks the API
// (the one `/api/demo` interception below only reports "demo mode off" so the
// non-demo cold-restore contract can be exercised in the same browser).

import { spawn } from "node:child_process";
import { mkdir, mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import {
  findModeTokens,
  installModeWatch,
  modeTokenRules,
  routingContextPattern,
} from "./demo-mode-watch.mjs";

const EXIT_OK = 0;
const EXIT_CHECK_FAILED = 1;
const EXIT_HARNESS = 2;
// The corrected suite runs more scenarios than the 2026-10-20 one (two extra
// streamed questions plus the navigation contract), so the whole-run budget
// is raised from 25 to 60 minutes. The launcher may still override it.
const DEFAULT_TIMEOUT_MS = 60 * 60 * 1_000;
// The real answering model's grounded turn takes 15s to 170s in this
// environment (measured from the demo's own turn receipts), so the answer
// wait covers the slowest observed turn. This is a LATENCY TOLERANCE, not an
// assertion: every check still asserts the same facts, citations and absence
// of a picker, and a turn that never answers still fails.
const QUESTION_TIMEOUT_MS = 240 * 1_000;
const TERMINAL_WAIT_MS = 6 * 60 * 1_000;
const RESET_TIMEOUT_MS = 20 * 60 * 1_000;
const NAV_TIMEOUT_MS = 15 * 1_000;
// 18 pre-existing checks plus the 8 routing/navigation checks below.
const EXPECTED_CHECK_COUNT = 26;
// The independent question groups are the fan-out unit; the stateful
// navigation group is a dependency of the sample worker, and the reset check
// runs after everything else. Six concurrent sessions is the supported
// ceiling: more sessions than that only add model contention.
const MAX_PARALLEL_WORKERS = 6;
const EXPECTED_URL = new URL("../fixtures/demo-corpus/expected.json", import.meta.url);
// Sessions that were opened and not yet confirmed closed. Signal cleanup and
// the normal finally both drain this set, so a failed worker cannot leave an
// orphan browser behind.
const ACTIVE_SESSIONS = new Set();

const USAGE = `Usage: node scripts/demo-browser-acceptance.mjs [options]

Options:
  --base-url=URL       loopback demo URL (env FIN_DEMO_E2E_BASE_URL, default http://127.0.0.1:8000)
  --timeout-ms=N       whole browser-run budget (default 3600000)
  --session=NAME       playwright-cli session prefix (default fin-demo-acceptance)
  --parallel=N         run the independent question groups in N concurrent
                       Playwright sessions (1-${MAX_PARALLEL_WORKERS}); the stateful
                       navigation checks follow the sample thread in one
                       session, and the demo reset runs last
  --serial             run every check in one session (default; debug path)
  --keep-open          leave the browser sessions open for debugging
  -h, --help           this text

Parallel workers share the same demo server. Each worker writes its own
structured JSON report into the run's temporary directory; the orchestrator
merges the 26 check rows, the watcher events, and the request summaries.
The launcher defaults to --parallel=5; a direct run defaults to serial.

Prerequisites: invoke through scripts/demo-browser-acceptance.sh; direct runs
without its test-database preflight are refused. Requires global playwright-cli.`;

function positiveInteger(value, name) {
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error(`--${name} must be a positive integer`);
  }
  return parsed;
}

function parseBaseUrl(value) {
  let parsed;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error("--base-url must be an absolute http(s) URL");
  }
  if (
    !["http:", "https:"].includes(parsed.protocol) ||
    !["127.0.0.1", "localhost"].includes(parsed.hostname.toLowerCase()) ||
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("--base-url must be a loopback http(s) origin without credentials or extra path data");
  }
  return parsed.toString().replace(/\/$/, "");
}

function parseArgument(opts, arg) {
  const simple = {
    "--keep-open": () => { opts.keepOpen = true; },
    "--serial": () => { opts.serial = true; },
    "--help": () => { opts.help = true; },
    "-h": () => { opts.help = true; },
  }[arg];
  if (simple) { simple(); return; }
  const separator = arg.indexOf("=");
  if (!arg.startsWith("--") || separator < 0) throw new Error(`unknown argument: ${arg}`);
  const flag = arg.slice(2, separator);
  const value = arg.slice(separator + 1);
  const handlers = {
    "base-url": (input) => { opts.baseUrl = input; },
    "timeout-ms": (input) => { opts.timeoutMs = positiveInteger(input, flag); },
    session: (input) => {
      if (!input.trim()) throw new Error("--session must not be empty");
      opts.session = input;
    },
    parallel: (input) => {
      const workers = positiveInteger(input, flag);
      if (workers > MAX_PARALLEL_WORKERS) {
        throw new Error(`--parallel must be between 1 and ${MAX_PARALLEL_WORKERS}`);
      }
      opts.parallel = workers;
    },
  };
  const handler = handlers[flag];
  if (!handler) throw new Error(`unknown argument: ${arg}`);
  handler(value);
}

function parseArgs(argv) {
  const opts = {
    baseUrl: process.env.FIN_DEMO_E2E_BASE_URL ?? "http://127.0.0.1:8000",
    timeoutMs: DEFAULT_TIMEOUT_MS,
    session: "fin-demo-acceptance",
    parallel: 0,
    serial: false,
    keepOpen: false,
    help: false,
  };
  for (const arg of argv) parseArgument(opts, arg);
  if (opts.serial && opts.parallel) {
    throw new Error("--serial and --parallel are mutually exclusive");
  }
  if (opts.serial) opts.parallel = 0;
  opts.baseUrl = parseBaseUrl(opts.baseUrl);
  return opts;
}

function runCli(bin, args, { cwd, timeoutMs }) {
  return new Promise((resolve) => {
    const child = spawn(bin, args, { cwd, stdio: ["ignore", "pipe", "pipe"] });
    let stdout = "";
    let stderr = "";
    let timedOut = false;
    const timer = setTimeout(() => {
      timedOut = true;
      child.kill("SIGKILL");
    }, timeoutMs);
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      clearTimeout(timer);
      resolve({ code: null, stdout, stderr: error.message, timedOut, spawnFailed: true });
    });
    child.on("close", (code) => {
      clearTimeout(timer);
      resolve({ code, stdout, stderr, timedOut, spawnFailed: false });
    });
  });
}

function parseRunCodeResult(stdout) {
  const payload = JSON.parse(stdout);
  if (payload.isError) throw new Error(payload.error ?? "playwright-cli reported an error");
  const value = payload.result ?? payload;
  return typeof value === "string" ? JSON.parse(value) : value;
}

/**
 * The browser snippet. Serial and parallel modes share the same helper
 * serialization; only the entry function differs (`runDemoAcceptance` runs
 * the whole flattened plan, `runDemoWorker` runs one worker group).
 */
function snippetFor(options, entry = "runDemoAcceptance") {
  const helpers = SERIALIZED_HELPERS.map(
    (helper) => `  const ${helper.name} = (${helper.toString()});`,
  ).join("\n");
  return `async (page) => {\n${helpers}\n  return await ${entry}(page, ${JSON.stringify(options)});\n}\n`;
}

function acceptanceAssert(condition, message) {
  if (!condition) throw new Error(message);
}

async function recordCheck(ctx, id, body) {
  const startedAt = Date.now();
  try {
    const detail = await body();
    ctx.checks.push({ id, pass: true, detail, ms: Date.now() - startedAt });
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    ctx.checks.push({
      id,
      pass: false,
      error: message.split("\n")[0].slice(0, 300),
      ms: Date.now() - startedAt,
    });
  }
}

async function answerText(article) {
  return ((await article.getByTestId("turn-answer-card").innerText()) ?? "").replace(/\s+/g, " ");
}

async function answerProse(article) {
  return ((await article.getByTestId("turn-answer-card").locator("p.whitespace-pre-wrap").first().innerText()) ?? "")
    .replace(/\s+/g, " ");
}

async function terminalArticle(ctx) {
  const article = ctx.page.locator("article").last();
  await article.getByTestId("turn-answer-card").waitFor({ state: "visible", timeout: ctx.cfg.questionTimeoutMs });
  await article.getByText(/^(完成|未完成|步数上限)/).waitFor({ state: "visible", timeout: ctx.cfg.questionTimeoutMs });
  return article;
}

async function demoQuestionButton(ctx, index = 0) {
  const button = ctx.page.getByTestId("demo-question").nth(index);
  await button.waitFor({ state: "visible", timeout: ctx.cfg.navTimeoutMs });
  acceptanceAssert(await button.getAttribute("aria-label"), `demo question ${index + 1} has no accessible label`);
  return button;
}

async function askSampleQuestion(ctx) {
  const button = await demoQuestionButton(ctx);
  const question = await button.getAttribute("aria-label");
  const eventsBefore = (await readWatchEvents(ctx)).length;
  const postsBefore = messagePostCount(ctx);
  await button.click();
  await ctx.page.waitForURL(/\/t\//, { timeout: ctx.cfg.navTimeoutMs });
  const threadUrl = ctx.page.url();
  const live = await waitForLiveAnswer(ctx, ctx.cfg.questionTimeoutMs, eventsBefore, postsBefore);
  acceptanceAssert(
    live.visibleBeforeResponse,
    `the example's answer was not visible while the send was still in flight${
      live.error ? ` (${live.error})` : ""
    }`,
  );
  const entered = (await readWatchEvents(ctx)).slice(eventsBefore);
  const loadingOverAnswer = entered.filter((event) => event.kind === "loading_over_answer");
  acceptanceAssert(
    loadingOverAnswer.length === 0,
    "the entry loading overlay painted over the example's answer",
  );
  return {
    article: await terminalArticle(ctx),
    question,
    threadUrl,
    liveAnswerObserved: live.observed,
    answerVisibleAt: live.answerVisibleAt,
    responseFinishedAt: live.responseFinishedAt,
    responseStatus: live.responseStatus,
    deltaMs: live.deltaMs,
    loadingOverAnswer: loadingOverAnswer.length,
    entryLoadingOverlay: entered.filter((event) => event.kind === "entry_loading_overlay").length,
  };
}

async function fillAndSend(ctx, question) {
  const textarea = ctx.page.locator("textarea").first();
  const send = ctx.page.getByTestId("composer-send");
  await textarea.fill(question);
  await send.waitFor({ state: "visible", timeout: ctx.cfg.navTimeoutMs });
  await ctx.page.waitForFunction(() => {
    const button = document.querySelector('[data-testid="composer-send"]');
    return button instanceof HTMLButtonElement && !button.disabled;
  }, undefined, { timeout: ctx.cfg.navTimeoutMs });
  const previousArticles = await ctx.page.locator("article").count();
  await send.click();
  await ctx.page.waitForFunction(
    (previous) => document.querySelectorAll("article").length > previous,
    previousArticles,
    { timeout: ctx.cfg.navTimeoutMs },
  );
  return await terminalArticle(ctx);
}

async function openFreshThread(ctx) {
  await ctx.page.goto(`${ctx.cfg.baseUrl}/`, { timeout: ctx.cfg.navTimeoutMs });
  await ctx.page.getByRole("button", { name: "+ 新建 thread" }).click();
  await ctx.page.waitForURL(/\/t\//, { timeout: ctx.cfg.navTimeoutMs });
}

async function expandSources(ctx, sources) {
  await sources.waitFor({ state: "visible", timeout: ctx.cfg.questionTimeoutMs });
  const collapsed = sources.locator('button[aria-expanded="false"]:not([aria-label^="显示引文"])');
  for (let attempt = 0; attempt < 16 && (await collapsed.count()) > 0; attempt += 1) {
    await collapsed.first().click();
  }
  return ((await sources.innerText()) ?? "").replace(/\s+/g, " ");
}

function assertSourceLocator(text, label) {
  const documentName = label.split(" · Page")[0];
  const pageNumber = /Page (\d+)/.exec(label)?.[1];
  const sectionNumber = /Section (\d+)/.exec(label)?.[1];
  acceptanceAssert(
    documentName && pageNumber && sectionNumber && text.includes(documentName) &&
    text.includes(`p.${pageNumber}`) && text.includes(`Section ${sectionNumber}`),
    `source list omitted the document, section, or page from ${label}`,
  );
}

async function assertInlineExcerpt(ctx, article) {
  // The marker list is built from the non-overlapping quote spans the answer
  // actually contains, so a later citation may legitimately be the first
  // one rendered. The claim under test is that an inline marker exists and
  // opens its own excerpt, not that citation #1 happens to be that marker.
  const marker = article.locator("button[data-superscript-index]").first();
  await marker.waitFor({ state: "visible", timeout: ctx.cfg.questionTimeoutMs });
  const panelId = await marker.getAttribute("aria-controls");
  acceptanceAssert(panelId, "inline citation marker has no excerpt target");
  await marker.click();
  const excerpt = article.locator(`#${panelId}`);
  await excerpt.waitFor({ state: "visible", timeout: ctx.cfg.navTimeoutMs });
  const quote = ((await excerpt.textContent()) ?? "").replace(/[“”]/g, "").trim();
  acceptanceAssert(quote.length > 0, "citation click did not reveal an excerpt");
}

async function citationSource(ctx, article, label) {
  const sources = article.locator('section[aria-label="Sources"]');
  assertSourceLocator(await expandSources(ctx, sources), label);
  await assertInlineExcerpt(ctx, article);
  return { source: label, excerptVisible: true };
}

async function assertDefaultReceiptHidden(ctx, questionId) {
  acceptanceAssert(
    (await ctx.page.getByTestId("task-panel").count()) === 0,
    `${questionId} exposed the internal task panel in the default view`,
  );
  acceptanceAssert(
    (await ctx.page.getByTestId("search-mode-receipt").count()) === 0,
    `${questionId} exposed the search-mode receipt in the default view`,
  );
}

async function answerGroundedQuestion(ctx, entry, factPatterns) {
  await openFreshThread(ctx);
  const article = await fillAndSend(ctx, entry.question);
  const answer = await answerText(article);
  acceptanceAssert(factPatterns.every((pattern) => pattern.test(answer)), `${entry.id} omitted one or more expected facts`);
  const source = await citationSource(ctx, article, entry.locator);
  acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, `${entry.id} unexpectedly requested clarification`);
  await assertDefaultReceiptHidden(ctx, entry.id);
  await finishQuestion(ctx, entry.id);
  return { expectedFacts: true, receiptHiddenByDefault: true, ...source };
}

/** Facts stay contiguous; the connective ``at`` before a time is prose. */
function answerStatesFact(answer, fact) {
  const normalize = (text) =>
    text.replace(/\s+at\s+/gi, " ").replace(/\s+/g, " ").trim();
  return normalize(answer).includes(normalize(fact));
}

async function checkExpectedDocumentQuestion(ctx, entry) {
  await recordCheck(ctx, `${entry.id}-expected-facts-citations-default-view`, async () => {
    await openFreshThread(ctx);
    const article = await fillAndSend(ctx, entry.question);
    const answer = await answerProse(article);
    for (const fact of entry.key_facts) {
      acceptanceAssert(answerStatesFact(answer, fact), `${entry.id} answer omitted expected fact: ${fact}`);
    }
    acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, `${entry.id} unexpectedly requested clarification`);
    acceptanceAssert((await article.getByTestId("choice-window").count()) === 0, `${entry.id} unexpectedly opened a search-mode choice`);

    const sources = article.locator('section[aria-label="Sources"]');
    const sourceText = await expandSources(ctx, sources);
    const expectedSources = [entry, ...(entry.additional_sources ?? [])];
    for (const source of expectedSources) assertSourceLocator(sourceText, source.locator);
    await assertInlineExcerpt(ctx, article);
    await assertDefaultReceiptHidden(ctx, entry.id);
    await finishQuestion(ctx, entry.id);
    return {
      expectedFacts: entry.key_facts.length,
      sourceLocators: expectedSources.map((source) => source.locator),
      inlineExcerpt: true,
      receiptHiddenByDefault: true,
    };
  });
}

/**
 * One check per document question id. The wrapper is a check like any other,
 * so the parallel plan and the serial plan count the same rows.
 */
function documentQuestionGroup(ids) {
  return ids.map((id) => {
    const check = async (ctx) => {
      await checkExpectedDocumentQuestion(ctx, ctx.byId[id]);
    };
    // The name is test/report metadata only; the source text is what gets
    // serialized into the browser snippet.
    Object.defineProperty(check, "name", { value: `check-${id}` });
    return check;
  });
}

/**
 * The run-code sandbox has no `URL` global (playwright-cli evaluates the
 * snippet in a bare V8 context), so origin/path are parsed by hand.
 */
function urlParts(value) {
  const text = String(value);
  const scheme = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.exec(text);
  let origin = "";
  let rest = text;
  if (scheme) {
    rest = text.slice(scheme[0].length);
    const slash = rest.indexOf("/");
    const authority = slash === -1 ? rest : rest.slice(0, slash);
    origin = scheme[0] + authority;
    rest = slash === -1 ? "/" : rest.slice(slash);
  }
  const cut = rest.search(/[?#]/);
  const path = cut === -1 ? rest : rest.slice(0, cut);
  return { origin, path };
}

async function restrictToAppOrigin(ctx) {
  const appOrigin = urlParts(ctx.cfg.baseUrl).origin;
  // WHY an init script instead of ``page.route("**/*")``: a registered
  // route keeps every request inside Playwright's interception path for the
  // whole session. The acceptance must observe the app's own stream, so the
  // origin guard lives in the page. It covers script-issued transport —
  // fetch, XHR, EventSource, WebSocket and sendBeacon — while the request
  // watch still records all traffic for the run summary. Subresource loads
  // and navigations are NOT covered by this guard.
  await ctx.page.addInitScript((origin) => {
    const appOriginOf = (input) => {
      try {
        const value = typeof input === "string"
          ? input
          : input && typeof input.url === "string"
            ? input.url
            : String(input);
        return new URL(value, window.location.href).origin;
      } catch {
        return null;
      }
    };
    const allowed = (input) => appOriginOf(input) === origin;
    const blocked = (door, input) =>
      new TypeError(`blocked ${door} outside the app origin: ${String(input)}`);
    const originalFetch = window.fetch;
    window.fetch = function (input, init) {
      // A blocked fetch rejects like a network fault, so an app that
      // handles failures through ``.catch`` observes this one the same way.
      if (!allowed(input)) return Promise.reject(blocked("fetch", input));
      return originalFetch.call(this, input, init);
    };
    const originalOpen = XMLHttpRequest.prototype.open;
    XMLHttpRequest.prototype.open = function (method, url, ...rest) {
      if (!allowed(url)) throw blocked("XMLHttpRequest", url);
      return originalOpen.call(this, method, url, ...rest);
    };
    for (const name of ["EventSource", "WebSocket"]) {
      const Original = window[name];
      if (!Original) continue;
      window[name] = class extends Original {
        constructor(url, options) {
          if (!allowed(url)) throw blocked(name, url);
          super(url, options);
        }
      };
    }
    const originalBeacon = navigator.sendBeacon && navigator.sendBeacon.bind(navigator);
    if (originalBeacon) {
      navigator.sendBeacon = function (url, data) {
        if (!allowed(url)) return false;
        return originalBeacon(url, data);
      };
    }
  }, appOrigin);
}

// ---------------------------------------------------------------------------
// The mode watch.
//
// The routing-disclosure vocabulary, the classification and the page-side
// scanner live in ``scripts/demo-mode-watch.mjs`` (imported above): they
// are one concern, and the scanner must stay a self-contained function
// body because the browser context has no module system. What stays here
// is the runner's side of the watch: reading the recorded events and
// waiting for them.
// ---------------------------------------------------------------------------

async function readWatchEvents(ctx) {
  const events = await ctx.page.evaluate(
    () => (window.__finDemoWatch ? window.__finDemoWatch.events() : []),
  );
  return Array.isArray(events) ? events : [];
}

/**
 * The fresh measurement pair since the caller's cursors: the first
 * answer-visible watcher event and the first message POST. Events and posts
 * from before the click cannot satisfy the measurement.
 */
async function liveAnswerSnapshot(ctx, eventsBefore, postsBefore) {
  const events = await readWatchEvents(ctx);
  const answerEvent = events.slice(eventsBefore).find((event) => event.kind === "answer_visible") || null;
  const posts = messagePosts(ctx).slice(postsBefore);
  return { answerEvent, post: posts.length > 0 ? posts[0] : null };
}

/** A failed send POST, or null while the request can still succeed. */
function liveAnswerFailure(post, answerEvent) {
  if (!post) return null;
  if (post.responseStatus !== null && (post.responseStatus < 200 || post.responseStatus >= 300)) {
    return {
      observed: false,
      answerVisibleAt: answerEvent ? answerEvent.at : null,
      responseFinishedAt: post.responseFinishedAt,
      responseStatus: post.responseStatus,
      visibleBeforeResponse: false,
      deltaMs: null,
      error: `the send answered HTTP ${post.responseStatus}`,
    };
  }
  if (post.responseFinishFailedAt !== null) {
    return {
      observed: false,
      answerVisibleAt: answerEvent ? answerEvent.at : null,
      responseFinishedAt: null,
      responseStatus: post.responseStatus,
      visibleBeforeResponse: false,
      deltaMs: null,
      error: `the send response failed: ${post.responseError || "unknown error"}`,
    };
  }
  return null;
}

/** The observed answer result, or null until both clocks have a value. */
function liveAnswerResult({ answerEvent, post }) {
  if (!post || post.responseFinishedAt === null || !answerEvent) return null;
  const deltaMs = post.responseFinishedAt - answerEvent.at;
  return {
    observed: true,
    answerVisibleAt: answerEvent.at,
    responseFinishedAt: post.responseFinishedAt,
    responseStatus: post.responseStatus,
    visibleBeforeResponse: deltaMs > 0,
    deltaMs,
  };
}

/** The unobserved result shape every failure/timeout path returns. */
function liveAnswerUnobserved(snapshot, error) {
  return {
    observed: false,
    answerVisibleAt: snapshot.answerEvent ? snapshot.answerEvent.at : null,
    responseFinishedAt: snapshot.post ? snapshot.post.responseFinishedAt : null,
    responseStatus: snapshot.post ? snapshot.post.responseStatus : null,
    visibleBeforeResponse: false,
    deltaMs: null,
    error,
  };
}

/**
 * The answer must be visible while the send's POST is still in flight.
 *
 * The answer card and its terminal status line usually land in ONE React
 * commit, so the DOM terminal marker cannot be the clock. The closing of the
 * POST response is: `answerVisibleAt` comes from the watcher's first
 * `answer_visible` event (page `Date.now()`) and `responseFinishedAt` from
 * `response.finished()` (Node `Date.now()`) — the two run on the same host
 * and share the epoch. `visibleBeforeResponse` is the claim.
 */
async function waitForLiveAnswer(ctx, timeoutMs, eventsBefore, postsBefore) {
  const deadline = Date.now() + timeoutMs;
  // A finished POST with no answer gets only a paint-latency grace; the full
  // question budget must not be spent waiting for what will never come.
  const finishedGraceMs = 5_000;
  let finishedSeenAt = null;
  let snapshot = { answerEvent: null, post: null };
  while (Date.now() < deadline) {
    snapshot = await liveAnswerSnapshot(ctx, eventsBefore, postsBefore);
    const failure = liveAnswerFailure(snapshot.post, snapshot.answerEvent);
    if (failure) return failure;
    const result = liveAnswerResult(snapshot);
    if (result) return result;
    if (snapshot.post && snapshot.post.responseFinishedAt !== null) {
      if (finishedSeenAt === null) finishedSeenAt = Date.now();
      if (Date.now() - finishedSeenAt >= finishedGraceMs) {
        return liveAnswerUnobserved(
          snapshot,
          "the send finished and no answer followed",
        );
      }
    }
    await sleep(ctx.page, 250);
  }
  return liveAnswerUnobserved(
    snapshot,
    snapshot.answerEvent
      ? "the answer was visible but the send response did not finish"
      : "timed out waiting for the answer and the send response",
  );
}

/**
 * Wait until the send's POST exists and its response has NOT finished. The
 * real model spends seconds streaming its thinking before the answer burst,
 * so this is the stable window in which the return-home scenario can
 * navigate away while the send is still in flight (the answer/terminal DOM
 * window is only tens of milliseconds wide).
 */
async function waitForSendInFlight(ctx, postsBefore, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const posts = messagePosts(ctx).slice(postsBefore);
    const post = posts.length > 0 ? posts[0] : null;
    if (post && post.responseFinishedAt === null && post.responseFinishFailedAt === null) return post;
    if (post) return null;
    await sleep(ctx.page, 100);
  }
  return null;
}

/** Wait for the in-flight POST record to carry either completion signal. */
async function waitForSendResponse(ctx, post, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (post.responseFinishedAt !== null || post.responseFinishFailedAt !== null) return post;
    await sleep(ctx.page, 100);
  }
  return null;
}

async function turnIsTerminal(ctx) {
  return await ctx.page.evaluate(
    () => (window.__finDemoWatch ? window.__finDemoWatch.isTerminal() : false),
  );
}

/**
 * The per-question routing gate: no routing window and no routing token may
 * have appeared since the last question, and the page as it stands now must
 * not render one either.
 */
async function finishQuestion(ctx, label) {
  const events = await readWatchEvents(ctx);
  const fresh = events.slice(ctx.watchCursor);
  ctx.watchCursor = events.length;
  const modeWindow = fresh.find((event) => event.kind === "mode_window");
  acceptanceAssert(!modeWindow, `${label}: a retrieval-mode choice window opened during the question`);
  const token = fresh.find((event) => event.kind === "mode_token");
  acceptanceAssert(!token, `${label}: an internal routing token appeared in the ${token ? token.surface : "page"}`);
  await assertNoModeDisclosure(ctx, label);
  return fresh.length;
}

/**
 * The rendered page must not carry an internal routing name, and no control
 * may offer a routing choice.
 */
async function assertNoModeDisclosure(ctx, label) {
  const surfaces = await ctx.page.evaluate(() => {
    const textOf = (selector) => Array.from(document.querySelectorAll(selector)).map((element) => element.textContent || "");
    let chrome = "";
    if (document.body) {
      const clone = document.body.cloneNode(true);
      clone.querySelectorAll('[data-testid="turn-answer-card"]').forEach((element) => element.remove());
      chrome = clone.textContent || "";
    }
    return {
      chrome,
      routing: [
        ...textOf('[data-testid="search-mode-receipt"]'),
        ...textOf('[data-testid="choice-window"]'),
        ...textOf('[data-testid="clarification-card"]'),
        ...textOf('[data-testid="document-choice-card"]'),
      ],
      prose: textOf('[data-testid="turn-answer-card"] p.whitespace-pre-wrap'),
      controls: Array.from(
        document.querySelectorAll('button, a, select, input, [role="button"], [role="option"], [role="radio"]'),
      ).map((element) => `${element.textContent || ""} ${element.getAttribute("aria-label") || ""}`),
    };
  });
  const strict = [];
  for (const text of [surfaces.chrome, ...surfaces.routing]) {
    for (const token of findModeTokens(text, { prose: false })) strict.push(token);
  }
  acceptanceAssert(
    strict.length === 0,
    `${label}: the rendered page disclosed an internal routing token (${[...new Set(strict)].join(", ")})`,
  );
  const proseTokens = [];
  for (const text of surfaces.prose) {
    for (const token of findModeTokens(text, { prose: true })) proseTokens.push(token);
  }
  acceptanceAssert(
    proseTokens.length === 0,
    `${label}: the answer prose disclosed an internal routing name (${[...new Set(proseTokens)].join(", ")})`,
  );
  const modeControls = surfaces.controls.filter(
    (text) => /检索档|搜索档/.test(text) || findModeTokens(text, { prose: false }).length > 0,
  );
  acceptanceAssert(modeControls.length === 0, `${label}: the page offered an internal routing control`);
}

/**
 * The developer receipt shows where the decision came from, never which tier
 * ran: a provenance line, a known decision tier, and no routing token.
 */
async function assertReceiptProvenance(ctx, label) {
  const receipt = ctx.page.getByTestId("search-mode-receipt");
  await receipt.waitFor({ state: "visible", timeout: ctx.cfg.navTimeoutMs });
  const text = ((await receipt.innerText()) ?? "").replace(/\s+/g, " ");
  acceptanceAssert(/检索档决策：/.test(text), `${label}: the developer receipt showed no decision provenance`);
  const tiers = ["规则命中", "判定命中", "沿用上次", "自动回退", "需要选择", "具名失败", "人工选择"];
  const shown = tiers.filter((tier) => text.includes(tier));
  acceptanceAssert(shown.length > 0, `${label}: the developer receipt named no known decision tier`);
  const tokens = findModeTokens(text, { prose: false });
  acceptanceAssert(
    tokens.length === 0,
    `${label}: the developer receipt disclosed an internal routing token (${tokens.join(", ")})`,
  );
  return { tier: shown[0], receipt: text.slice(0, 240) };
}

async function assertHomePage(ctx, label) {
  const { page, cfg } = ctx;
  const path = urlParts(page.url()).path;
  acceptanceAssert(path === "/", `${label}: expected the demo home page, found ${path}`);
  await page.getByTestId("demo-mode-badge").waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
  await page.getByTestId("demo-question").first().waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
}

async function demoReturnLink(ctx) {
  const link = ctx.page.getByRole("link", { name: "返回演示首页", exact: true });
  if ((await link.count()) > 0) return link;
  const button = ctx.page.getByRole("button", { name: "返回演示首页", exact: true });
  acceptanceAssert((await button.count()) > 0, "the demo conversation offered no 返回演示首页 link");
  return button;
}

async function sidebarHomeLink(ctx) {
  const link = ctx.page.getByRole("link", { name: "首页", exact: true });
  acceptanceAssert((await link.count()) > 0, "the sidebar offered no 首页 link");
  return link;
}

async function sleep(page, ms) {
  await page.waitForTimeout(ms);
}

/**
 * Every request the browser made, with its parsed body when it had one and,
 * for responses, the status plus the time the response body finished.
 *
 * The response must update the SAME record its request created: one POST is
 * one record, and its response completion is the "send is still in flight"
 * clock the answer measurement compares against. `response.finished()` is
 * the only signal that an SSE body has closed.
 */
function installRequestWatch(page) {
  const requests = [];
  const byRequest = new Map();
  page.on("request", (request) => {
    let path = "";
    try {
      path = urlParts(request.url()).path;
    } catch {
      path = request.url();
    }
    let body = null;
    try {
      body = request.postData();
    } catch {
      body = null;
    }
    let json = null;
    if (body) {
      try {
        json = JSON.parse(body);
      } catch {
        json = null;
      }
    }
    const record = {
      method: request.method(),
      path,
      body,
      json,
      responseStatus: null,
      responseFinishedAt: null,
      responseFinishFailedAt: null,
      responseError: null,
    };
    requests.push(record);
    byRequest.set(request, record);
  });
  page.on("requestfailed", (request) => {
    const record = byRequest.get(request);
    if (!record || record.responseFinishedAt !== null || record.responseFinishFailedAt !== null) return;
    const failure = request.failure();
    record.responseFinishFailedAt = Date.now();
    record.responseError = failure && failure.errorText ? failure.errorText : "request failed";
  });
  page.on("response", (response) => {
    const record = byRequest.get(response.request());
    if (!record) return;
    record.responseStatus = response.status();
    response.finished().then(
      (error) => {
        if (error) {
          // A failed response resolves (rather than rejects) with its error.
          record.responseFinishFailedAt = Date.now();
          record.responseError = error.message ? error.message : String(error);
          return;
        }
        record.responseFinishedAt = Date.now();
      },
      (error) => {
        record.responseFinishFailedAt = Date.now();
        record.responseError = error && error.message ? error.message : String(error);
      },
    );
  });
  return requests;
}

/** One definition of a send: both the check helpers and the worker summary
 * count the same requests. */
function isMessagePost(request) {
  return request.method === "POST" && /\/api\/threads\/[^/]+\/messages$/.test(request.path);
}

function messagePosts(ctx) {
  return ctx.requests.filter(isMessagePost);
}

function messagePostCount(ctx) {
  return messagePosts(ctx).length;
}

/** The last-thread marker this browser profile actually carries, if any. */
async function readStoredLastThread(page) {
  const entries = await page.evaluate(() => {
    const found = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      found.push([key, window.localStorage.getItem(key)]);
    }
    return found;
  });
  const exact = entries.find(([key, value]) => key === "fin.lastThread" && value);
  if (exact) return exact[1];
  const similar = entries.find(([key, value]) => /last.*thread/i.test(key) && value);
  return similar ? similar[1] : null;
}

/**
 * The last-thread marker for the cold-start checks. A parallel worker has its
 * own browser profile, so the sample worker publishes the marker it observed
 * (or no marker); the worker falls back to the sample thread id it was given.
 */
async function readLastThreadMarker(ctx) {
  const stored = await readStoredLastThread(ctx.page);
  if (stored) return stored;
  if (ctx.lastThreadMarker) return ctx.lastThreadMarker;
  if (ctx.sampleThreadId) {
    await ctx.page.evaluate(
      (threadId) => window.localStorage.setItem("fin.lastThread", threadId),
      ctx.sampleThreadId,
    );
    return ctx.sampleThreadId;
  }
  return null;
}

/**
 * Wait until the server-side thread carries a terminal turn. This is how the
 * harness proves that returning home neither cancelled nor duplicated a send.
 */
async function waitForThreadTerminal(ctx, threadUrl, timeoutMs) {
  const threadId = urlParts(threadUrl).path.split("/").filter(Boolean).pop();
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const state = await ctx.page.evaluate(async (id) => {
      const response = await fetch(`/api/threads/${encodeURIComponent(id)}`);
      if (!response.ok) return { ok: false };
      const detail = await response.json();
      const turns = Array.isArray(detail.turns) ? detail.turns : [];
      const last = turns.length > 0 ? turns[turns.length - 1] : null;
      return {
        ok: true,
        turns: turns.length,
        status: last ? last.status ?? null : null,
        answered: last ? Boolean(last.answer) : false,
      };
    }, threadId);
    if (state.ok && state.status) return state;
    await sleep(ctx.page, 1000);
  }
  return null;
}

async function checkDemoHome(ctx) {
  await recordCheck(ctx, "demo-home", async () => {
    const { page, cfg } = ctx;
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    const badge = page.getByTestId("demo-mode-badge");
    await badge.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    acceptanceAssert((await badge.innerText()).includes("演示模式"), "demo badge text is missing");
    const sampleButtons = page.getByTestId("demo-question");
    const labels = await sampleButtons.evaluateAll((buttons) => buttons.map((button) => button.getAttribute("aria-label")));
    acceptanceAssert(labels.length === 8 && labels.every(Boolean), "home must show eight accessible sample questions");
    acceptanceAssert(/ZEUS.*年假/.test(labels[0] ?? ""), "first sample should ask about ZEUS annual leave");
    await page.getByTestId("demo-reset-button").waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    return { badge: true, sampleQuestions: [ctx.byId.q1.id, ctx.byId.q6.id, ctx.byId.q13.id], resetControl: true };
  });
}

async function checkQ1(ctx) {
  await recordCheck(ctx, "q1-answer-citation-default-view", async () => {
    const { page, cfg, byId } = ctx;
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    await page.evaluate(() => localStorage.removeItem("fin.developer-mode"));
    await page.reload({ timeout: cfg.navTimeoutMs });
    const {
      article, question, threadUrl, liveAnswerObserved, deltaMs, loadingOverAnswer, entryLoadingOverlay,
    } = await askSampleQuestion(ctx);
    ctx.sampleThreadUrl = threadUrl;
    ctx.sampleThreadId = urlParts(threadUrl).path.split("/").filter(Boolean).pop();
    const answer = await answerText(article);
    acceptanceAssert(/\b22\b/.test(answer), "q1 answer omitted the 22-day annual leave fact");
    acceptanceAssert(/\b5\b/.test(answer), "q1 answer omitted the 5-day carry-over limit");
    acceptanceAssert(/(?:31\s+December|December\s+31|31\s+Dec|12\s*月\s*31\s*日)/i.test(answer), "q1 answer omitted when excess unused leave is forfeited");
    const source = await citationSource(ctx, article, byId.q1.locator);
    acceptanceAssert((await page.getByTestId("task-panel").count()) === 0, "default view exposed the internal receipt panel");
    acceptanceAssert((await page.getByTestId("search-mode-receipt").count()) === 0, "default view exposed search-mode receipt");
    acceptanceAssert((await article.getByTestId("choice-window").count()) === 0, "clear q1 unexpectedly opened a search-mode choice window");
    acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, "clear q1 unexpectedly asked a clarification");
    // The model may cite supporting documents in any order; this check is
    // about the viewer opening the document the question asks for, not about
    // which citation happened to come first.
    const sourceLink = article
      .getByTestId("citation-source-link")
      .filter({ hasText: "ZEUS Employee Handbook" })
      .first();
    await sourceLink.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    await sourceLink.click();
    await page.waitForURL(/\/demo\/source\?/, { timeout: cfg.navTimeoutMs });
    const viewer = page.getByTestId("demo-source-viewer");
    await viewer.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    acceptanceAssert((await viewer.getByTestId("demo-source-title").innerText()).includes("ZEUS Employee Handbook"), "source viewer opened the wrong document");
    acceptanceAssert((await viewer.getByTestId("demo-source-section").innerText()).includes("Section 1"), "source viewer omitted the cited section");
    acceptanceAssert(/\bPage\s+1\b/.test(await viewer.getByTestId("demo-source-page").innerText()), "source viewer omitted the cited page");
    const citedChunk = await viewer.getByTestId("demo-source-content").innerText();
    acceptanceAssert(/22 days per calendar year/.test(citedChunk) && /5 unused annual leave days/.test(citedChunk), "source viewer omitted the cited chunk");
    await finishQuestion(ctx, "q1");
    return {
      sampleQuestion: question,
      expectedFacts: true,
      ...source,
      receiptHidden: true,
      noClarification: true,
      liveAnswerObserved,
      answerToResponseDeltaMs: deltaMs,
      loadingOverAnswer,
      entryLoadingOverlay,
    };
  });
}

async function checkQ2(ctx) {
  await recordCheck(ctx, "q2-product-answer-citation", () => answerGroundedQuestion(ctx, ctx.byId.q2, [
    /USD\s*12,450/i, /2024-03-04/, /40\s*Gbps/i, /1\.8\s*milliseconds?/i,
  ]));
}

async function checkQ4(ctx) {
  await recordCheck(ctx, "q4-board-resolution-answer-citation", () => answerGroundedQuestion(ctx, ctx.byId.q4, [
    /USD\s*0\.85/i, /2024-06-14/, /\b(?:5|five)\b.{0,30}\b(?:in favou?r|for)\b/i,
    /\b(?:2|two)\b.{0,20}\bagainst\b/i, /\b(?:0|zero)\b.{0,20}\babstentions?\b/i,
  ]));
}

async function checkQ6(ctx) {
  await recordCheck(ctx, "q6-unsupported-refusal", async () => {
    await openFreshThread(ctx);
    const article = await fillAndSend(ctx, ctx.byId.q6.question);
    const answer = await answerText(article);
    acceptanceAssert(answer.includes("资料中未找到相关内容"), "q6 did not use the required unsupported-answer refusal");
    acceptanceAssert(!/\b(?:USD|\$)\s?\d|\b(?:CEO|director|led by)\b/i.test(answer), "q6 fabricated a budget or leader");
    acceptanceAssert((await article.locator('section[aria-label="Sources"]').count()) === 0, "unsupported q6 displayed fabricated sources");
    acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, "clear unsupported q6 was turned into a clarification");
    await finishQuestion(ctx, "q6");
    return { explicitRefusal: true, noFabricatedFacts: true, noSources: true };
  });
}

async function checkQ13(ctx) {
  await recordCheck(ctx, "q13-same-thread-follow-up", async () => {
    const { page, byId } = ctx;
    await page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    await openFreshThread(ctx);
    const first = await fillAndSend(ctx, byId.q13.question);
    const threadUrl = page.url();
    const firstAnswer = await answerText(first);
    acceptanceAssert(/\b22\b/.test(firstAnswer), "q13 first answer omitted 22 annual leave days");
    const firstReceipt = await assertReceiptProvenance(ctx, "q13 first turn");
    const firstSource = await citationSource(ctx, first, byId.q13.locator);
    const second = await fillAndSend(ctx, byId.q13.follow_up.question);
    acceptanceAssert(page.url() === threadUrl, "follow-up left the original thread");
    const followUpAnswer = await answerText(second);
    acceptanceAssert(/\b5\b/.test(followUpAnswer), "follow-up omitted the 5-day carry-over limit");
    acceptanceAssert(/31\s*(December|Dec)|December\s+31|12\s*月\s*31/i.test(followUpAnswer), "follow-up omitted the excess-day expiry");
    const secondSource = await citationSource(ctx, second, byId.q13.follow_up.locator);
    const secondReceipt = await assertReceiptProvenance(ctx, "q13 follow-up turn");
    acceptanceAssert((await second.getByTestId("choice-window").count()) === 0, "same-job follow-up reopened the mode picker");
    await finishQuestion(ctx, "q13");
    return {
      sameThread: true,
      firstCitation: firstSource.source,
      followUpCitation: secondSource.source,
      firstProvenance: firstReceipt.tier,
      followUpProvenance: secondReceipt.tier,
    };
  });
}

/**
 * The restored q5 guard: an underspecified revenue ask must not invent
 * ZEUS's 412,300,000. The content clarification offers the catalog's own
 * document titles and the turn carries no fabricated figure; picking the
 * quarterly summary then answers the original ask from that document with
 * its citation.
 */
async function checkQ5AmbiguousRevenue(ctx) {
  await recordCheck(ctx, "q5-underspecified-revenue-clarification", async () => {
    const { page, cfg, byId } = ctx;
    await page.evaluate(() => localStorage.removeItem("fin.developer-mode"));
    await page.reload({ timeout: cfg.navTimeoutMs });
    await openFreshThread(ctx);
    const first = await fillAndSend(ctx, "What was revenue?");
    const card = first.getByTestId("clarification-card");
    await card.waitFor({ state: "visible", timeout: cfg.questionTimeoutMs });
    const turnText = ((await first.innerText()) ?? "").replace(/\s+/g, " ");
    const clarification = ((await card.innerText()) ?? "").replace(/\s+/g, " ");
    acceptanceAssert(
      clarification.includes("ZEUS Quarterly Financial Summary"),
      "the underspecified revenue ask did not offer the quarterly summary",
    );
    acceptanceAssert(
      !turnText.includes("412,300,000"),
      "underspecified revenue ask guessed ZEUS's quarterly revenue",
    );
    acceptanceAssert(
      !clarification.includes("412,300,000"),
      "the clarification card carried ZEUS's quarterly revenue",
    );
    acceptanceAssert(
      (await first.locator('section[aria-label="Sources"]').count()) === 0,
      "underspecified revenue ask answered with unsupported citations",
    );
    acceptanceAssert(
      (await first.getByTestId("choice-window").count()) === 0,
      "the underspecified revenue ask opened a routing choice window instead of asking a content question",
    );
    const threadUrl = page.url();
    await assertDefaultReceiptHidden(ctx, byId.q5.id);
    await page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    const before = await page.locator("article").count();
    await card
      .getByRole("button", { name: "ZEUS Quarterly Financial Summary", exact: true })
      .click();
    await first.getByTestId("clarification-submit").click();
    await page.waitForFunction(
      (count) => document.querySelectorAll("article").length > count,
      before,
      { timeout: cfg.navTimeoutMs },
    );
    const clarified = await terminalArticle(ctx);
    acceptanceAssert(page.url() === threadUrl, "the clarified revenue answer left the original thread");
    const answer = await answerText(clarified);
    acceptanceAssert(
      answer.includes("412,300,000"),
      "answer after clarification omitted the expected ZEUS revenue",
    );
    const source = await citationSource(ctx, clarified, byId.q5.locator);
    const provenance = await assertReceiptProvenance(ctx, "q5");
    await finishQuestion(ctx, "q5 ambiguous revenue");
    return {
      offeredQuarterlySummary: true,
      noGuessedFigure: true,
      clarifiedAnswer: true,
      citation: source.source,
      provenance: provenance.tier,
    };
  });
}

async function checkAmbiguousClarification(ctx) {
  await recordCheck(ctx, "ambiguous-clarification-then-answer", async () => {
    const { page, cfg, byId } = ctx;
    await page.evaluate(() => localStorage.removeItem("fin.developer-mode"));
    await page.reload({ timeout: cfg.navTimeoutMs });
    await openFreshThread(ctx);
    // The annual-leave question reaches several ZEUS documents, so the
    // ambiguous follow-up is decided by real retrieval hits rather than
    // by an internal company/period slot. The card of real document
    // titles is the content clarification this check exists for.
    const first = await fillAndSend(ctx, byId.q1.question);
    acceptanceAssert(/22/.test(await answerText(first)), "the opening annual-leave answer changed");
    const followUp = await fillAndSend(ctx, "那别的公司呢");
    const card = followUp.getByTestId("clarification-card");
    await card.waitFor({ state: "visible", timeout: cfg.questionTimeoutMs });
    const clarification = (await card.innerText()).replace(/\s+/g, " ");
    acceptanceAssert(
      clarification.includes("ZEUS Employee Handbook"),
      "the clarification card did not offer the real catalog document title",
    );
    acceptanceAssert(
      !/company|period|公司[:：]|期间[:：]/i.test(clarification),
      "the clarification card leaked internal slot names",
    );
    acceptanceAssert((await followUp.getByTestId("choice-window").count()) === 0, "the ambiguous ask opened a routing choice window instead of asking a content question");
    acceptanceAssert((await followUp.locator('section[aria-label="Sources"]').count()) === 0, "ambiguous follow-up answered with unsupported citations");
    const threadUrl = page.url();
    // The default view must not carry the internal receipt at all.
    await assertDefaultReceiptHidden(ctx, byId.q1.id);
    // The decision is carried by the live terminal status frame, so the
    // developer switch is turned on BEFORE the clarified answer: the
    // receipt is then read from the live turn, not from a reloaded history
    // row (which the host does not persist).
    await page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    const before = await page.locator("article").count();
    // The handoff's ONE free-text reply: this row answers through Other,
    // never by clicking an offered title (q5 owns the click reply). The
    // field lives INSIDE the selected Other option, so Other is picked
    // first and the typed answer is what the host receives.
    const otherAnswer = "只看 ZEUS Employee Handbook";
    await followUp.getByTestId("clarification-choice-other").click();
    const otherText = followUp.getByTestId("clarification-other-text");
    await otherText.fill(otherAnswer);
    acceptanceAssert(
      (await otherText.inputValue()) === otherAnswer,
      "the Other field did not keep the free-text clarification answer",
    );
    await followUp.getByTestId("clarification-submit").click();
    await page.waitForFunction(
      (count) => document.querySelectorAll("article").length > count,
      before,
      { timeout: cfg.navTimeoutMs },
    );
    const clarified = await terminalArticle(ctx);
    acceptanceAssert(page.url() === threadUrl, "clarification answer left the original thread");
    const answer = await answerText(clarified);
    acceptanceAssert(answer.includes("22"), "the free-text clarification answer omitted the annual-leave fact");
    acceptanceAssert(
      ((await clarified.innerText()) ?? "").includes("annual leave"),
      "the free-text reply turn did not carry the original question as context",
    );
    acceptanceAssert(
      ((await clarified.innerText()) ?? "").includes(otherAnswer),
      "the free-text reply turn did not carry the typed clarification answer",
    );
    const source = await citationSource(ctx, clarified, byId.q1.locator);
    const provenance = await assertReceiptProvenance(ctx, "clarify reply");
    await finishQuestion(ctx, "ambiguous clarification");
    return {
      offeredCatalogTitles: true,
      freeTextReply: otherAnswer,
      clarifiedAnswer: true,
      citation: source.source,
      provenance: provenance.tier,
    };
  });
}

/**
 * The q3 change-process facts, matched tolerantly: the corpus states them as
 * exact English strings ("2024-12-20T00:00:00Z", "every Tuesday at 15:00
 * UTC", "at least 4 of the 5 voting members"), while the answer may phrase
 * the same fact in Chinese.
 */
function q3BehaviourPatterns() {
  return [
    /2024-12-20/,
    /2025-01-06/,
    /15:00\s*UTC|Tuesday|每周二|周二/i,
    /(?:at least|至少|不少于)\s*4|\b4\s*(?:of|out\s+of|\/)\s*(?:the\s*)?5\b|4\s*[名位]/i,
  ];
}

async function checkLexicalReceipt(ctx) {
  await recordCheck(ctx, "developer-only-search-mode-receipt", async () => {
    const { page, cfg, byId } = ctx;
    await page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    await openFreshThread(ctx);
    const article = await fillAndSend(ctx, byId.q3.question);
    // The old check only read the mode name off the receipt. The routing
    // decision is no longer visible, so the check now proves the behaviour the
    // lookup produced: the change-process facts and the cited source.
    const answer = await answerText(article);
    for (const pattern of q3BehaviourPatterns()) {
      acceptanceAssert(pattern.test(answer), "q3 answer omitted one or more change-process facts");
    }
    const source = await citationSource(ctx, article, byId.q3.locator);
    acceptanceAssert((await article.getByTestId("choice-window").count()) === 0, "q3 unexpectedly opened a routing choice window");
    const panel = page.getByTestId("task-panel");
    await panel.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    const provenance = await assertReceiptProvenance(ctx, "q3");
    await finishQuestion(ctx, "q3");
    return {
      developerMode: true,
      receiptVisible: true,
      behaviour: "change-freeze facts and citation",
      expectedFacts: true,
      ...source,
      provenance: provenance.tier,
    };
  });
}

async function checkQ14(ctx) {
  await recordCheck(ctx, "q14-unsupported-sql-refusal", async () => {
    await ctx.page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    await openFreshThread(ctx);
    const article = await fillAndSend(ctx, ctx.byId.q14.question);
    const answer = await answerText(article);
    acceptanceAssert(answer.includes("资料中未找到相关内容"), "q14 did not refuse unsupported ZEUS invoice data");
    acceptanceAssert(!/\b(?:USD|\$)\s?\d|\b(?:invoice|supplier)\s+[A-Z]/i.test(answer), "q14 fabricated invoice facts");
    acceptanceAssert((await article.locator('section[aria-label="Sources"]').count()) === 0, "unsupported q14 displayed sources");
    acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, "clear q14 was turned into a clarification");
    const provenance = await assertReceiptProvenance(ctx, "q14");
    await finishQuestion(ctx, "q14");
    return { explicitRefusal: true, provenance: provenance.tier, noFabricatedFacts: true, noSources: true };
  });
}

async function checkQ15(ctx) {
  await recordCheck(ctx, "q15-positive-sql-ledger-answer", async () => {
    const { page, byId } = ctx;
    await page.evaluate(() => localStorage.setItem("fin.developer-mode", "1"));
    await openFreshThread(ctx);
    const article = await fillAndSend(ctx, byId.q15.question);
    const answer = ((await article.getByTestId("turn-answer-card").locator("p.whitespace-pre-wrap").first().innerText()) ?? "")
      .replace(/\s+/g, " ");
    for (const fact of byId.q15.key_facts) {
      acceptanceAssert(answer.includes(fact), `q15 answer omitted expected ledger fact: ${fact}`);
    }
    acceptanceAssert((await article.getByTestId("clarification-card").count()) === 0, "q15 unexpectedly requested clarification");
    acceptanceAssert((await article.getByTestId("choice-window").count()) === 0, "q15 unexpectedly opened a search-mode choice");

    const provenance = await assertReceiptProvenance(ctx, "q15");

    const sources = article.locator('section[aria-label="Sources"]');
    await expandSources(ctx, sources);
    const citationButtons = sources.getByRole("button", { name: /^显示引文：/ });
    acceptanceAssert((await citationButtons.count()) === byId.q15.rows.length, "q15 did not cite every returned ledger row");
    for (const expectedRow of byId.q15.rows) {
      const citation = sources.getByRole("button", {
        name: new RegExp(`^显示引文:.*${expectedRow.contract_id}|^显示引文：.*${expectedRow.contract_id}`),
      });
      await citation.click();
      const row = citation.locator("..");
      const identity = await row.getByTestId("database-row-identity").innerText();
      const fields = await row.getByTestId("database-row-fields").innerText();
      acceptanceAssert(identity.includes(`contract_id=${expectedRow.contract_id}`), `q15 citation omitted ${expectedRow.contract_id}`);
      for (const value of [expectedRow.counterparty, expectedRow.expiry_date, expectedRow.notice_by]) {
        acceptanceAssert(fields.includes(value), `q15 row ${expectedRow.contract_id} omitted ${value}`);
      }
    }
    await finishQuestion(ctx, "q15");
    return {
      answerMatchesAllExpectedRows: true,
      provenance: provenance.tier,
      structuredRowCitations: byId.q15.rows.length,
    };
  });
}

/**
 * Returning home while the send is still in flight must keep the send alive
 * and must not duplicate it. The scenario waits for the send's POST to be
 * observed but NOT finished (the model's thinking stream is seconds wide,
 * unlike the tens-of-milliseconds answer/terminal DOM window), navigates
 * home, and then proves both the response and the server-side answer
 * survived the navigation.
 */
async function checkReturnHomeWhileStreaming(ctx) {
  await recordCheck(ctx, "demo-return-home-while-streaming-keeps-send", async () => {
    const { page, cfg } = ctx;
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    await page.getByTestId("demo-mode-badge").waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    const postsBefore = messagePostCount(ctx);
    const button = await demoQuestionButton(ctx, 0);
    await button.click();
    await page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs });
    const threadUrl = page.url();
    const sendPost = await waitForSendInFlight(ctx, postsBefore, cfg.navTimeoutMs);
    acceptanceAssert(sendPost, "the send finished before the return-home scenario could navigate away");
    // No loader assertion here: this scenario navigates during the model's
    // thinking stream, before any answer exists, so a loader-over-answer
    // event would be impossible. The entry check in askSampleQuestion owns
    // that claim.
    const link = await demoReturnLink(ctx);
    await link.click();
    await page.waitForURL((url) => urlParts(url).path === "/", { timeout: cfg.navTimeoutMs });
    await assertHomePage(ctx, "after the demo return link");
    await sleep(ctx.page, 1000);
    acceptanceAssert(urlParts(page.url()).path === "/", "the demo return link did not stay on the home page");
    const posts = messagePostCount(ctx) - postsBefore;
    acceptanceAssert(posts === 1, `the example send posted ${posts} questions instead of one`);
    const finished = await waitForSendResponse(ctx, sendPost, cfg.terminalWaitMs);
    acceptanceAssert(
      finished && finished.responseFinishedAt !== null,
      "returning home cancelled the send response",
    );
    const terminal = await waitForThreadTerminal(ctx, threadUrl, cfg.terminalWaitMs);
    acceptanceAssert(terminal && terminal.status, "the send was cancelled by the return home");
    acceptanceAssert(terminal.answered, "the send finished but the thread carried no answer");
    await finishQuestion(ctx, "demo return home while streaming");
    return {
      threadUrl,
      sendInFlightAtNavigation: true,
      responseFinishedAt: finished.responseFinishedAt,
      messagePosts: posts,
      terminalStatus: terminal ? terminal.status : null,
      terminalAnswered: Boolean(terminal.answered),
      stayedHome: true,
    };
  });
}

async function checkSidebarHomeLink(ctx) {
  await recordCheck(ctx, "sidebar-home-link-stays-home", async () => {
    const { page, cfg } = ctx;
    acceptanceAssert(ctx.sampleThreadUrl, "no session was available for the sidebar home check");
    await page.goto(ctx.sampleThreadUrl, { timeout: cfg.navTimeoutMs });
    await page.locator("article").last().getByTestId("turn-answer-card")
      .waitFor({ state: "visible", timeout: cfg.questionTimeoutMs });
    const link = await sidebarHomeLink(ctx);
    await link.click();
    await page.waitForURL((url) => urlParts(url).path === "/", { timeout: cfg.navTimeoutMs });
    await assertHomePage(ctx, "after the sidebar home link");
    await sleep(ctx.page, 1000);
    acceptanceAssert(urlParts(page.url()).path === "/", "the sidebar home link did not stay on the home page");
    await finishQuestion(ctx, "sidebar home link");
    return { session: ctx.sampleThreadUrl, stayedHome: true };
  });
}

async function checkBrowserBackStaysHome(ctx) {
  await recordCheck(ctx, "browser-back-stays-home", async () => {
    const { page, cfg } = ctx;
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    await page.getByTestId("demo-mode-badge").waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    const button = await demoQuestionButton(ctx, 1);
    await button.click();
    await page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs });
    const threadUrl = page.url();
    const historyLength = await page.evaluate(() => window.history.length);
    await page.goBack();
    let backHome = true;
    try {
      await page.waitForURL((url) => urlParts(url).path === "/", { timeout: cfg.navTimeoutMs });
    } catch {
      backHome = false;
    }
    acceptanceAssert(backHome, "the browser Back found no home history entry to return to");
    await assertHomePage(ctx, "after the browser Back");
    await sleep(ctx.page, 1500);
    acceptanceAssert(urlParts(page.url()).path === "/", "the browser Back returned into the thread instead of the home page");
    await finishQuestion(ctx, "browser back to the demo home");
    return { threadUrl, historyLength, homeEntryPreserved: true, stayedHome: true };
  });
}

async function checkDemoColdHomeDoesNotRestore(ctx) {
  await recordCheck(ctx, "demo-cold-home-does-not-restore-saved-thread", async () => {
    const { page, cfg } = ctx;
    const marker = await readLastThreadMarker(ctx);
    acceptanceAssert(marker, "no last-thread marker was available for the cold demo home check");
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    await assertHomePage(ctx, "cold demo home");
    await sleep(ctx.page, 2500);
    acceptanceAssert(urlParts(page.url()).path === "/", "the cold demo home restored the saved thread");
    await finishQuestion(ctx, "cold demo home");
    return { marker, stayedHome: true };
  });
}

async function checkNonDemoColdHomeRestores(ctx) {
  await recordCheck(ctx, "non-demo-cold-home-restores-last-thread", async () => {
    const { page, cfg } = ctx;
    const marker = await readLastThreadMarker(ctx);
    acceptanceAssert(marker, "no last-thread marker was available for the non-demo cold home check");
    await page.route("**/api/demo", async (route) => {
      await route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({ enabled: false }),
      });
    });
    try {
      await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
      let restored = true;
      try {
        await page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs });
      } catch {
        restored = false;
      }
      acceptanceAssert(restored, "the non-demo cold home did not restore the saved thread");
      acceptanceAssert(
        urlParts(page.url()).path === `/t/${marker}`,
        `the non-demo cold home restored ${urlParts(page.url()).path} instead of the marked thread`,
      );
      return { marker, restoredTo: urlParts(page.url()).path };
    } finally {
      await page.unroute("**/api/demo");
    }
  });
}

async function checkColdThreadDeepLink(ctx) {
  await recordCheck(ctx, "cold-thread-deep-link-loads-thread", async () => {
    const { page, cfg } = ctx;
    acceptanceAssert(ctx.sampleThreadUrl, "no thread was available for the cold deep-link check");
    await page.goto(ctx.sampleThreadUrl, { timeout: cfg.navTimeoutMs });
    const article = await terminalArticle(ctx);
    const answer = await answerText(article);
    acceptanceAssert(answer.length > 0, "the cold deep link loaded an empty thread");
    await finishQuestion(ctx, "cold thread deep link");
    return { threadUrl: ctx.sampleThreadUrl, loaded: true };
  });
}

/**
 * The cross-run disclosure assertion over the watcher events of one browser
 * profile (serial) or of every worker (parallel). Keeping the claim in one
 * pure function is what lets the orchestrator aggregate without re-deriving
 * a weaker check.
 */
function disclosureSummary(events) {
  const modeWindows = events.filter((event) => event.kind === "mode_window");
  acceptanceAssert(modeWindows.length === 0, `the run opened ${modeWindows.length} retrieval-mode choice window(s)`);
  const tokens = events.filter((event) => event.kind === "mode_token");
  acceptanceAssert(
    tokens.length === 0,
    `the run rendered an internal routing token in the ${tokens.length > 0 ? tokens[0].surface : "page"}`,
  );
  const clarifications = events.filter((event) => event.kind === "content_clarification");
  acceptanceAssert(
    clarifications.length > 0,
    "the run never showed a content clarification, so the mode/content discrimination was not exercised",
  );
  const otherWindows = events.filter((event) => event.kind === "choice_window");
  const count = (kind) => events.filter((event) => event.kind === kind).length;
  return {
    modeWindows: 0,
    modeTokens: 0,
    contentClarifications: clarifications.length,
    otherChoiceWindows: otherWindows.length,
    answerVisible: count("answer_visible"),
    liveAnswers: count("live_answer"),
    loadingOverAnswers: count("loading_over_answer"),
    entryLoadingOverlays: count("entry_loading_overlay"),
  };
}

async function checkNoModeDisclosureThroughout(ctx) {
  await recordCheck(ctx, "no-mode-window-or-text-throughout-run", async () =>
    disclosureSummary(await readWatchEvents(ctx)));
}

/**
 * The request records a worker contributes to the run-wide tiers assertion.
 * Only the shape is kept (no bodies), so merging worker reports stays small
 * and never carries question text between sessions.
 */
function summarizeRequests(requests) {
  const messages = requests.filter(isMessagePost);
  const withTiers = requests.filter(
    (request) => request.json && Object.prototype.hasOwnProperty.call(request.json, "tiers"),
  );
  return {
    observedRequests: requests.length,
    messagePosts: messages.length,
    invalidMessagePosts: messages.filter(
      (request) => !(request.json && typeof request.json.question === "string" && request.json.question.length > 0),
    ).length,
    messageKeys: [...new Set(messages.flatMap((request) => Object.keys(request.json ?? {})))].sort(),
    tiersFields: withTiers.length,
    tiersPaths: [...new Set(withTiers.map((request) => request.path))],
  };
}

/** The run-wide request assertion over one or more worker summaries. */
function requestTiersSummary(summaries) {
  const list = summaries.filter(Boolean);
  acceptanceAssert(list.length > 0, "the run produced no request evidence");
  const sum = (field) => list.reduce((total, summary) => total + (summary[field] ?? 0), 0);
  const withTiers = sum("tiersFields");
  acceptanceAssert(withTiers === 0, `${withTiers} request body/bodies carried a tiers field`);
  const messages = sum("messagePosts");
  acceptanceAssert(messages > 0, "the run sent no question, so the request shape was never observed");
  acceptanceAssert(
    sum("invalidMessagePosts") === 0,
    "a message request carried no question text",
  );
  const keys = [...new Set(list.flatMap((summary) => summary.messageKeys ?? []))].sort();
  return {
    observedRequests: sum("observedRequests"),
    messagePosts: messages,
    tiersFields: 0,
    messageKeys: keys,
  };
}

async function checkRequestBodiesCarryNoTiers(ctx) {
  await recordCheck(ctx, "request-bodies-carry-no-tiers", async () =>
    requestTiersSummary([summarizeRequests(ctx.requests)]));
}

async function checkDemoReset(ctx) {
  await recordCheck(ctx, "demo-reset-control", async () => {
    const { page, cfg } = ctx;
    await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
    const reset = page.getByTestId("demo-reset-button");
    await reset.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    await reset.click();
    const confirm = page.getByTestId("demo-reset-confirm");
    await confirm.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    const resetResponsePromise = page.waitForResponse(
      (response) => response.url() === `${cfg.baseUrl}/api/demo/reset` && response.request().method() === "POST",
      { timeout: cfg.resetTimeoutMs },
    );
    await confirm.click();
    const response = await resetResponsePromise;
    acceptanceAssert(response.status() === 200, `demo reset returned HTTP ${response.status()}`);
    const payload = await response.json();
    acceptanceAssert(payload && payload.status === "reset", "demo reset response did not confirm completion");
    const result = page.getByTestId("demo-reset-result");
    await result.waitFor({ state: "visible", timeout: cfg.navTimeoutMs });
    acceptanceAssert((await result.innerText()).includes("演示数据已重置"), "reset success copy was not shown");
    acceptanceAssert((await page.getByTestId("demo-reset-error").count()) === 0, "reset also rendered an error");
    return { resetConfirmed: true, successCopy: true };
  });
}

/** Every acceptance question the fixture must carry, named in the failure. */
function assertFixtureQuestions(byId) {
  const required = [
    "q1", "q2", "q3", "q4", "q5", "q6", "q7", "q8", "q9", "q10", "q11", "q12", "q13", "q14", "q15",
  ];
  const missing = required.filter((id) => !byId[id]);
  acceptanceAssert(missing.length === 0, `expected.json is missing acceptance questions: ${missing.join(", ")}`);
  acceptanceAssert(Boolean(byId.q13?.follow_up), "expected.json is missing the q13 follow-up question");
}

/**
 * The one plan for the whole suite. Each entry is a worker-sized group of
 * checks; the stateful group depends on the sample thread published by the
 * sample worker, and the reset check is deliberately NOT a group here so the
 * orchestrator can run it after every other session is closed.
 *
 * Group sizes (checks): sample 2, documents-a 3, documents-b 3, grounded 5,
 * follow-ups 4, stateful 6 = 23; the two aggregated checks and the reset
 * check complete the 26 rows. The grounded group precedes the follow-ups in
 * the serial flatten because q2/q4/q6 assert the default (non-developer)
 * view; the follow-up group is what turns the developer switch on.
 */
function acceptanceGroups() {
  return [
    { id: "sample", publishesSample: true, checks: [checkDemoHome, checkQ1] },
    { id: "documents-a", checks: documentQuestionGroup(["q7", "q8", "q9"]) },
    { id: "documents-b", checks: documentQuestionGroup(["q10", "q11", "q12"]) },
    { id: "grounded", checks: [checkQ2, checkQ4, checkQ6, checkQ14, checkQ15] },
    { id: "follow-ups", checks: [checkQ13, checkQ5AmbiguousRevenue, checkAmbiguousClarification, checkLexicalReceipt] },
    {
      id: "stateful",
      stateful: true,
      checks: [
        checkReturnHomeWhileStreaming,
        checkSidebarHomeLink,
        checkBrowserBackStaysHome,
        checkDemoColdHomeDoesNotRestore,
        checkNonDemoColdHomeRestores,
        checkColdThreadDeepLink,
      ],
    },
  ];
}

/** Checks that need every worker's evidence; the orchestrator runs them. */
function acceptanceAggregateChecks() {
  return [checkNoModeDisclosureThroughout, checkRequestBodiesCarryNoTiers];
}

/** The reset rewrites demo data, so it always runs last, alone. */
function acceptanceResetChecks() {
  return [checkDemoReset];
}

function resetWorkerGroup() {
  return { id: "reset", reset: true, checks: acceptanceResetChecks() };
}

/** The serial fallback: the same groups, flattened, aggregates before reset. */
function serialSequence() {
  const sequence = [];
  for (const group of acceptanceGroups()) sequence.push(...group.checks);
  sequence.push(...acceptanceAggregateChecks(), ...acceptanceResetChecks());
  return sequence;
}

function workerGroupChecks(groupId) {
  const group = [...acceptanceGroups(), resetWorkerGroup()].find((candidate) => candidate.id === groupId);
  acceptanceAssert(group, `unknown worker group ${groupId}`);
  return group.checks;
}

function groupPublishesSample(groupId) {
  const group = acceptanceGroups().find((candidate) => candidate.id === groupId);
  return Boolean(group && group.publishesSample);
}

async function runDemoAcceptance(page, cfg) {
  const byId = Object.fromEntries(cfg.expected.questions.map((entry) => [entry.id, entry]));
  assertFixtureQuestions(byId);
  const ctx = { page, cfg, byId, checks: [], watchCursor: 0, requests: [] };
  await restrictToAppOrigin(ctx);
  await installModeWatch(page, { storageKey: cfg.watchStorageKey });
  ctx.requests = installRequestWatch(page);
  for (const check of serialSequence()) {
    await check(ctx);
  }
  return { checks: ctx.checks };
}

/**
 * One parallel worker: its own browser profile, its own watcher storage key,
 * and the subset of checks its group names. Stateful workers receive the
 * sample thread the sample worker published, because they cannot share its
 * localStorage.
 */
async function runDemoWorker(page, cfg) {
  const byId = Object.fromEntries(cfg.expected.questions.map((entry) => [entry.id, entry]));
  assertFixtureQuestions(byId);
  const ctx = {
    page,
    cfg,
    byId,
    checks: [],
    watchCursor: 0,
    requests: [],
    sampleThreadUrl: cfg.sampleThreadUrl ?? null,
    sampleThreadId: cfg.sampleThreadId ?? null,
    lastThreadMarker: cfg.lastThreadMarker ?? null,
  };
  await restrictToAppOrigin(ctx);
  await installModeWatch(page, { storageKey: `${cfg.watchStorageKey}.${cfg.workerGroup}` });
  ctx.requests = installRequestWatch(page);
  // A fresh worker browser starts at about:blank, where the first check's
  // `localStorage` read throws SecurityError. Every worker starts same-origin
  // instead; the init-script origin guard above was installed before the
  // first document loads.
  await page.goto(`${cfg.baseUrl}/`, { timeout: cfg.navTimeoutMs });
  for (const check of workerGroupChecks(cfg.workerGroup)) {
    await check(ctx);
  }
  const published = groupPublishesSample(cfg.workerGroup)
    ? {
      sampleThreadUrl: ctx.sampleThreadUrl ?? null,
      sampleThreadId: ctx.sampleThreadId ?? null,
      lastThreadMarker: await readStoredLastThread(page),
    }
    : null;
  return {
    checks: ctx.checks,
    watchEvents: await readWatchEvents(ctx),
    requestSummary: summarizeRequests(ctx.requests),
    published,
  };
}

/**
 * Merge the worker reports into the 26-row evidence. Every worker's watcher
 * events and request summary feed the two aggregated rows, so the parallel
 * run keeps the whole-run claims the serial run made.
 */
async function mergeWorkerReports(reports, resetReport = null) {
  const checks = [];
  const watchEvents = [];
  const requestSummaries = [];
  const missingEvidence = [];
  // The reset session is part of the run even though its row is printed
  // last: its watcher and request evidence still feed the run-wide claims.
  const evidenceReports = resetReport ? [...reports, resetReport] : reports;
  for (const report of reports) {
    checks.push(...(Array.isArray(report.checks) ? report.checks : []));
  }
  for (const report of evidenceReports) {
    if (Array.isArray(report.watchEvents)) watchEvents.push(...report.watchEvents);
    else missingEvidence.push(`${report.worker}: no watcher evidence`);
    if (report.requestSummary) requestSummaries.push(report.requestSummary);
    else missingEvidence.push(`${report.worker}: no request evidence`);
  }
  const disclosureCtx = { checks: [] };
  await recordCheck(disclosureCtx, "no-mode-window-or-text-throughout-run", async () => {
    acceptanceAssert(missingEvidence.length === 0, missingEvidence.join("; "));
    return disclosureSummary(watchEvents);
  });
  checks.push(disclosureCtx.checks[0]);
  const tiersCtx = { checks: [] };
  await recordCheck(tiersCtx, "request-bodies-carry-no-tiers", async () => {
    acceptanceAssert(missingEvidence.length === 0, missingEvidence.join("; "));
    return requestTiersSummary(requestSummaries);
  });
  checks.push(tiersCtx.checks[0]);
  if (resetReport) checks.push(...(Array.isArray(resetReport.checks) ? resetReport.checks : []));
  return { checks, watchEvents };
}

const SERIALIZED_HELPERS = [
  acceptanceAssert,
  recordCheck,
  answerText,
  answerProse,
  answerStatesFact,
  terminalArticle,
  demoQuestionButton,
  askSampleQuestion,
  fillAndSend,
  openFreshThread,
  expandSources,
  assertSourceLocator,
  assertInlineExcerpt,
  citationSource,
  assertDefaultReceiptHidden,
  answerGroundedQuestion,
  checkExpectedDocumentQuestion,
  documentQuestionGroup,
  urlParts,
  restrictToAppOrigin,
  modeTokenRules,
  routingContextPattern,
  findModeTokens,
  installModeWatch,
  readWatchEvents,
  liveAnswerSnapshot,
  liveAnswerFailure,
  liveAnswerResult,
  liveAnswerUnobserved,
  waitForLiveAnswer,
  waitForSendInFlight,
  waitForSendResponse,
  turnIsTerminal,
  finishQuestion,
  assertNoModeDisclosure,
  assertReceiptProvenance,
  assertHomePage,
  demoReturnLink,
  sidebarHomeLink,
  sleep,
  installRequestWatch,
  isMessagePost,
  messagePosts,
  messagePostCount,
  readStoredLastThread,
  readLastThreadMarker,
  waitForThreadTerminal,
  q3BehaviourPatterns,
  assertFixtureQuestions,
  checkDemoHome,
  checkQ1,
  checkQ2,
  checkQ4,
  checkQ6,
  checkQ13,
  checkQ5AmbiguousRevenue,
  checkAmbiguousClarification,
  checkLexicalReceipt,
  checkQ14,
  checkQ15,
  checkReturnHomeWhileStreaming,
  checkSidebarHomeLink,
  checkBrowserBackStaysHome,
  checkDemoColdHomeDoesNotRestore,
  checkNonDemoColdHomeRestores,
  checkColdThreadDeepLink,
  disclosureSummary,
  checkNoModeDisclosureThroughout,
  summarizeRequests,
  requestTiersSummary,
  checkRequestBodiesCarryNoTiers,
  checkDemoReset,
  acceptanceGroups,
  acceptanceAggregateChecks,
  acceptanceResetChecks,
  resetWorkerGroup,
  serialSequence,
  workerGroupChecks,
  groupPublishesSample,
  runDemoAcceptance,
  runDemoWorker,
];

function printReport(checks, { baseUrl, session }) {
  const width = Math.max(...checks.map((entry) => entry.id.length));
  console.log("fin browser acceptance — ZEUS demo");
  console.log(`  target       ${baseUrl}`);
  console.log(`  harness      playwright-cli (session ${session})`);
  console.log("");
  for (const entry of checks) {
    const verdict = entry.pass ? "PASS" : "FAIL";
    const duration = typeof entry.ms === "number" ? ` ${(entry.ms / 1000).toFixed(1)}s` : "";
    const detail = entry.error ?? JSON.stringify(entry.detail ?? {});
    console.log(`[${verdict}] ${entry.id.padEnd(width)}${duration}  ${detail}`);
  }
  const passed = checks.filter((entry) => entry.pass).length;
  console.log("");
  console.log(`verdict: ${passed === checks.length ? "PASS" : "FAIL"} — ${passed}/${checks.length} checks passed`);
}

function fail(message) {
  console.error(`[FAIL] harness  ${message}`);
  return EXIT_HARNESS;
}

async function main(argv) {
  let opts;
  try {
    opts = parseArgs(argv);
  } catch (error) {
    console.error(`demo-browser-acceptance: ${error.message}\n\n${USAGE}`);
    return EXIT_HARNESS;
  }
  if (opts.help) return showUsage();
  return await runAcceptance(opts);
}

function showUsage() {
  console.log(USAGE);
  return EXIT_OK;
}

async function loadExpected() {
  try {
    const expected = JSON.parse(await readFile(EXPECTED_URL, "utf8"));
    if (!Array.isArray(expected?.questions)) throw new Error("expected.json has no questions array");
    return expected;
  } catch (error) {
    throw new Error(`could not load demo expected questions: ${error.message}`);
  }
}

async function runAcceptance(opts) {
  if (process.env.FIN_DEMO_ACCEPTANCE_READY !== "1") {
    return fail("run through scripts/demo-browser-acceptance.sh so its _test database preflight runs first");
  }
  if (!(await preflightDemoServer(opts.baseUrl))) {
    return fail("loopback target did not pass the demo readiness and no-redirect preflight");
  }
  let expected;
  try {
    expected = await loadExpected();
  } catch (error) {
    return fail(error.message);
  }
  const workDir = await mkdtemp(path.join(tmpdir(), "fin-demo-acceptance-"));
  const removeSignalHandlers = installSignalCleanup(opts, workDir);
  let status = EXIT_HARNESS;
  try {
    status = await runBrowser(opts, expected, workDir);
  } catch (error) {
    console.error(`[FAIL] harness  ${error && error.message ? error.message : String(error)}`);
    status = EXIT_HARNESS;
  } finally {
    removeSignalHandlers();
    const closed = await closeBrowser(opts, workDir);
    await rm(workDir, { recursive: true, force: true });
    if (!closed && status === EXIT_OK) status = EXIT_HARNESS;
  }
  return status;
}

async function preflightDemoServer(baseUrl) {
  try {
    const urls = [new URL("/api/demo", baseUrl), new URL("/", baseUrl)];
    const responses = [];
    for (const url of urls) {
      const response = await fetch(url, {
        redirect: "manual",
        signal: AbortSignal.timeout(NAV_TIMEOUT_MS),
      });
      if (response.status !== 200 || response.url !== url.href) return false;
      responses.push(response);
    }
    const status = await responses[0].json();
    await responses[1].body?.cancel();
    return status?.enabled === true;
  } catch {
    return false;
  }
}

function buildBrowserOptions(opts, expected) {
  return {
    baseUrl: opts.baseUrl,
    expected,
    navTimeoutMs: NAV_TIMEOUT_MS,
    questionTimeoutMs: QUESTION_TIMEOUT_MS,
    terminalWaitMs: TERMINAL_WAIT_MS,
    resetTimeoutMs: RESET_TIMEOUT_MS,
    // A per-run key keeps a previous run's watcher findings (same browser
    // session, same origin) out of this run's evidence. Parallel workers
    // append their group id, so two profiles can never share a key.
    watchStorageKey: `fin.demoAcceptance.watch.${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
  };
}

async function runBrowser(opts, expected, workDir) {
  const options = buildBrowserOptions(opts, expected);
  if (opts.parallel > 0) return await runParallelBrowser(opts, options, workDir);
  return await runSerialBrowser(opts, options, workDir);
}

async function runSerialBrowser(opts, options, workDir) {
  const bin = process.env.PLAYWRIGHT_CLI || "playwright-cli";
  const sessionArgs = [`-s=${opts.session}`];
  ACTIVE_SESSIONS.add(opts.session);
  const opened = await runCli(bin, [...sessionArgs, "open", "about:blank"], {
    cwd: workDir,
    timeoutMs: 120_000,
  });
  if (opened.spawnFailed || opened.code !== 0) return fail("could not open a Playwright browser");
  const run = await executeSnippet({ bin, sessionArgs, workDir, options, timeoutMs: opts.timeoutMs });
  return reportRun(run, opts);
}

/**
 * A bounded worker pool: at most ``limit`` Playwright sessions run at once.
 * The sample worker is awaited so the stateful worker can start as soon as
 * its sample thread exists, without waiting for the slower question groups.
 */
function createWorkerPool(limit) {
  const queue = [];
  let active = 0;
  const idleWaiters = [];
  const settle = () => {
    if (active === 0 && queue.length === 0) {
      idleWaiters.splice(0).forEach((resolve) => resolve());
    }
  };
  const pump = () => {
    while (active < limit && queue.length > 0) {
      const entry = queue.shift();
      active += 1;
      Promise.resolve().then(entry.task).then(
        (value) => {
          active -= 1;
          entry.resolve(value);
          pump();
        },
        (error) => {
          active -= 1;
          entry.reject(error);
          pump();
        },
      );
    }
    settle();
  };
  return {
    run(task) {
      return new Promise((resolve, reject) => {
        queue.push({ task, resolve, reject });
        pump();
      });
    },
    drain() {
      if (active === 0 && queue.length === 0) return Promise.resolve();
      return new Promise((resolve) => idleWaiters.push(resolve));
    },
  };
}

/**
 * One worker session. It opens its own browser profile, runs its group's
 * checks through the shared snippet, writes a structured report (checks,
 * watcher events, request summary, published sample state), and always closes
 * its session. The report file is the unit the orchestrator merges.
 */
async function openWorkerBrowser(bin, session, workerDir, deadline) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) throw new Error("the browser-run budget expired before this worker started");
  const opened = await runCli(bin, [`-s=${session}`, "open", "about:blank"], {
    cwd: workerDir,
    timeoutMs: Math.min(120_000, remaining),
  });
  if (opened.spawnFailed || opened.code !== 0) throw new Error("could not open a Playwright browser");
}

async function closeWorkerBrowser(bin, session, workerDir) {
  const closed = await runCli(bin, [`-s=${session}`, "close"], {
    cwd: workerDir,
    timeoutMs: 60_000,
  });
  // Keep the session registered when the close failed: closeBrowser
  // retries the whole set in the parallel finally and in runAcceptance.
  if (!closed.spawnFailed && closed.code === 0) ACTIVE_SESSIONS.delete(session);
}

/** The options one worker's snippet sees, including the sample it publishes. */
function workerSnippetOptions(options, group, published) {
  // The snippet appends the worker group to the watcher storage key, so
  // two profiles can never read each other's findings.
  return {
    ...options,
    workerGroup: group.id,
    sampleThreadUrl: published ? published.sampleThreadUrl : null,
    sampleThreadId: published ? published.sampleThreadId : null,
    lastThreadMarker: published ? published.lastThreadMarker : null,
  };
}

/** The worker report fields, with a non-list treated as empty. */
function projectWorkerReport(parsed) {
  return {
    checks: Array.isArray(parsed.checks) ? parsed.checks : [],
    watchEvents: Array.isArray(parsed.watchEvents) ? parsed.watchEvents : [],
    requestSummary: parsed.requestSummary ?? null,
    published: parsed.published ?? null,
  };
}

/** Run one group's snippet and project its report fields. */
async function executeWorkerGroup({ bin, session, workerDir, options, group, published, deadline }) {
  const workerOptions = workerSnippetOptions(options, group, published);
  const run = await executeSnippet({
    bin,
    sessionArgs: [`-s=${session}`],
    workDir: workerDir,
    options: workerOptions,
    timeoutMs: Math.max(1, deadline - Date.now()),
    entry: "runDemoWorker",
    filename: `worker-${group.id}-snippet.js`,
  });
  if (run.timedOut) throw new Error("the worker exceeded the browser-run budget");
  if (run.spawnFailed || run.code !== EXIT_OK) {
    throw new Error(`playwright-cli exited with code ${run.code}`);
  }
  let parsed;
  try {
    parsed = parseRunCodeResult(run.stdout);
  } catch (error) {
    throw new Error(`unreadable worker report: ${error && error.message ? error.message : String(error)}`);
  }
  return projectWorkerReport(parsed);
}

async function runWorkerGroup({ bin, opts, options, workDir, group, published = null, deadline }) {
  const session = `${opts.session}-${group.id}`;
  const workerDir = path.join(workDir, `worker-${group.id}`);
  await mkdir(workerDir, { recursive: true });
  const reportPath = path.join(workerDir, "report.json");
  const startedAt = Date.now();
  const report = {
    worker: group.id,
    session,
    checks: [],
    watchEvents: [],
    requestSummary: null,
    published: null,
    error: null,
    durationMs: 0,
  };
  ACTIVE_SESSIONS.add(session);
  try {
    await openWorkerBrowser(bin, session, workerDir, deadline);
    Object.assign(
      report,
      await executeWorkerGroup({ bin, session, workerDir, options, group, published, deadline }),
    );
  } catch (error) {
    report.error = error && error.message ? error.message : String(error);
    console.error(`[FAIL] worker ${group.id}  ${report.error}`);
  } finally {
    if (!opts.keepOpen) await closeWorkerBrowser(bin, session, workerDir);
    report.durationMs = Date.now() - startedAt;
    await writeFile(reportPath, `${JSON.stringify(report, null, 2)}\n`, "utf8");
  }
  // Merge the persisted artifact, not a second in-memory shape: the report
  // file is the worker-to-orchestrator contract.
  return JSON.parse(await readFile(reportPath, "utf8"));
}

/**
 * The parallel run: question workers share the demo server, the stateful
 * worker starts once the sample worker publishes its thread, every session is
 * closed, and only then the reset check (which rewrites demo data) runs.
 */
async function runParallelBrowser(opts, options, workDir) {
  const bin = process.env.PLAYWRIGHT_CLI || "playwright-cli";
  const groups = acceptanceGroups();
  const independent = groups.filter((group) => !group.stateful);
  const stateful = groups.find((group) => group.stateful);
  acceptanceAssert(independent.some((group) => group.publishesSample), "the plan has no sample worker");
  acceptanceAssert(stateful, "the plan has no stateful worker group");
  const sampleIndex = independent.findIndex((group) => group.publishesSample);
  const deadline = Date.now() + opts.timeoutMs;
  const pool = createWorkerPool(opts.parallel);
  let reports = [];
  try {
    const running = independent.map((group) => pool.run(() => runWorkerGroup({
      bin, opts, options, workDir, group, deadline,
    })));
    const sampleReport = await running[sampleIndex];
    const statefulPromise = pool.run(() => runWorkerGroup({
      bin,
      opts,
      options,
      workDir,
      group: stateful,
      deadline,
      published: sampleReport.published,
    }));
    const independentReports = await Promise.all(running);
    const statefulReport = await statefulPromise;
    await pool.drain();
    reports = [...independentReports, statefulReport];
  } finally {
    // Every worker closes its own session; this is the belt-and-braces pass
    // for a worker that died between opening and its own close.
    await closeBrowser(opts, workDir);
  }
  const resetReport = await runWorkerGroup({
    bin, opts, options, workDir, group: resetWorkerGroup(), deadline,
  });
  const merged = await mergeWorkerReports(reports, resetReport);
  const workerErrors = [...reports, resetReport]
    .filter((report) => report.error)
    .map((report) => `${report.worker}: ${report.error}`);
  return reportChecks(merged.checks, opts, workerErrors);
}

async function executeSnippet({ bin, sessionArgs, workDir, options, timeoutMs, entry = "runDemoAcceptance", filename = "demo-acceptance-snippet.js" }) {
  const snippetPath = path.join(workDir, filename);
  await writeFile(snippetPath, snippetFor(options, entry), "utf8");
  return await runCli(bin, [...sessionArgs, "--raw", "run-code", `--filename=${snippetPath}`], {
    cwd: workDir,
    timeoutMs,
  });
}

/** The parsed acceptance report, or the raw CLI output when it cannot be
 * read. The diagnostic dump lives here so the reporting flow stays a
 * straight line. */
function readRunReport(run) {
  try {
    return { report: parseRunCodeResult(run.stdout) };
  } catch {
    console.error("=== DIAGNOSTIC: raw CLI stdout (first 4000 chars) ===");
    console.error(String(run.stdout || "").slice(0, 4000));
    console.error("=== DIAGNOSTIC: raw CLI stderr (first 4000 chars) ===");
    console.error(String(run.stderr || "").slice(0, 4000));
    console.error("=== DIAGNOSTIC: exit code " + run.code + " ===");
    return { report: null };
  }
}

/**
 * Print the merged 26-row table and decide the exit code. A worker-level
 * failure (its session never produced a report) is a harness failure, not a
 * check failure; a partial table is still printed as evidence.
 */
function reportChecks(checks, opts, workerErrors = []) {
  const sessionLabel = opts.parallel > 0
    ? `${opts.session} (${opts.parallel} parallel workers)`
    : opts.session;
  printReport(checks, { baseUrl: opts.baseUrl, session: sessionLabel });
  if (checks.length !== EXPECTED_CHECK_COUNT) {
    console.error(`[FAIL] harness  expected ${EXPECTED_CHECK_COUNT} checks, got ${checks.length}`);
  }
  for (const error of workerErrors) console.error(`[FAIL] worker  ${error}`);
  if (workerErrors.length > 0) return EXIT_HARNESS;
  if (checks.length !== EXPECTED_CHECK_COUNT) return EXIT_CHECK_FAILED;
  return checks.every((entry) => entry.pass) ? EXIT_OK : EXIT_CHECK_FAILED;
}

function reportRun(run, opts) {
  if (run.timedOut) return fail(`browser acceptance exceeded ${opts.timeoutMs}ms`);
  if (run.spawnFailed || run.code !== EXIT_OK) return fail("Playwright browser acceptance could not complete");
  const { report } = readRunReport(run);
  if (!report) return fail("Playwright returned an unreadable acceptance report");
  const checks = Array.isArray(report.checks) ? report.checks : [];
  return reportChecks(checks, opts);
}

/** Close every session this run opened, retrying once. */
async function closeBrowser(opts, workDir) {
  if (opts.keepOpen) return true;
  const bin = process.env.PLAYWRIGHT_CLI || "playwright-cli";
  let allClosed = true;
  for (const session of [...ACTIVE_SESSIONS]) {
    let closed = false;
    for (let attempt = 0; attempt < 2 && !closed; attempt += 1) {
      const result = await runCli(bin, [`-s=${session}`, "close"], { cwd: workDir, timeoutMs: 60_000 });
      closed = !result.spawnFailed && result.code === 0;
      if (!closed && attempt === 0) await new Promise((resolve) => setTimeout(resolve, 250));
    }
    if (closed) {
      ACTIVE_SESSIONS.delete(session);
    } else {
      allClosed = false;
      console.error(`[FAIL] harness  could not close Playwright session ${session}`);
    }
  }
  return allClosed;
}

/**
 * Ctrl-C or a supervisor's TERM must not leave browser sessions or the
 * temporary report directory behind. The normal path removes these handlers
 * and does the same cleanup in ``runAcceptance``.
 */
function installSignalCleanup(opts, workDir) {
  let cleaning = false;
  const clean = async (code) => {
    if (cleaning) return;
    cleaning = true;
    await closeBrowser(opts, workDir);
    await rm(workDir, { recursive: true, force: true });
    process.exit(code);
  };
  const handlers = {
    SIGINT: () => { void clean(130); },
    SIGTERM: () => { void clean(143); },
    SIGHUP: () => { void clean(129); },
  };
  for (const [signal, handler] of Object.entries(handlers)) process.once(signal, handler);
  return () => {
    for (const [signal, handler] of Object.entries(handlers)) process.removeListener(signal, handler);
  };
}

// Pure plan/merge surface for the meta-tests. Importing this module does not
// run the harness: main only starts when this file is the process entry.
export {
  EXPECTED_CHECK_COUNT,
  MAX_PARALLEL_WORKERS,
  acceptanceGroups,
  acceptanceAggregateChecks,
  acceptanceResetChecks,
  serialSequence,
  mergeWorkerReports,
  disclosureSummary,
  requestTiersSummary,
  summarizeRequests,
  parseArgs,
  snippetFor,
  answerStatesFact,
};

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  process.exit(await main(process.argv.slice(2)));
}
