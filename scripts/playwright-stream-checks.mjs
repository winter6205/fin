// Browser e2e checks for streaming answer stability and layout
// (scripted-provider lane, `specs/workspace.md` Amendment 2026-08-26).
//
// This file is an ES module. `scripts/run-stream-checks.mjs` imports it and
// then ships `runStreamChecks.toString()` into `playwright-cli run-code`,
// which evaluates the function text in the browser-driver process. The body
// must therefore stay self-contained: no imports, no references to
// module-scope bindings, no dependency on this file's path at run time.
//
// Every check reports a verdict. A thrown error inside a check is recorded as
// a failure for that check id — never a skip, never an aborted run.

// Checks that run on every invocation, in report order.
// ``1-kb-corpus`` was renamed to ``1-home-chrome`` and
// ``6-status-strip`` to ``6-boot-status``: ``specs/workbench-
// retrieve-product-seam.md`` retired the /kb corpus grid and the
// /monitor status panel (see each check's comment), so the old names
// pinned surfaces that no longer exist.
const BASE_CHECK_IDS = [
  "1-home-chrome",
  "2-stream-no-retract",
  "4-dom-separation",
  "5-trace-collapse",
  "3-reload-persist",
  "6-boot-status",
];

// Runs only when the workspace under test replays a transcript that
// bounces the citation gate (`FIN_SCRIPTED_SCENARIO=bounce`). Declared
// conditionally rather than always, because a lane that cannot produce
// a bounce would otherwise report a permanent FAIL for a check it was
// never asked to run.
const BOUNCE_CHECK_ID = "7-bounce-no-blank";
const SCROLL_CHECK_ID = "7-stream-scroll-follow";
const SCROLL_LIFECYCLE_CHECK_ID = "8-scroll-follow-lifecycle";

/** Ids the runner must see a verdict for, given the same options the
 * check function is called with. An id declared here and missing from
 * the report is a failure, never a skip. */
export function declaredCheckIds(options = {}) {
  return [
    ...BASE_CHECK_IDS,
    ...(options.requireStreamGrowth
      ? [SCROLL_CHECK_ID, SCROLL_LIFECYCLE_CHECK_ID]
      : []),
    ...(options.expectBounce ? [BOUNCE_CHECK_ID] : []),
  ];
}

export const CHECK_IDS = BASE_CHECK_IDS;

export default async function runStreamChecks(page, options = {}) {
  const cfg = {
    baseUrl: "http://127.0.0.1:8000",
    question: "What was Apple's total revenue in Q3 2022?",
    navTimeoutMs: 10_000,
    pollIntervalMs: 1_000,
    pollCount: 120,
    // Opt-in (scripted lane): the provider paces its deltas, so the
    // answer MUST be seen growing across polls and MUST reach the
    // bubble before the terminal status line. Off by default — against
    // a live provider a whole reply can land inside one poll tick, and
    // that is not a fidelity regression.
    requireStreamGrowth: false,
    // Opt-in: the workspace under test replays a transcript whose
    // first answer is ungrounded, so the citation gate bounces it and
    // the retry carries the wire's `reset`. Only then can the browser
    // watch the T3 boundary.
    expectBounce: false,
    bounceQuestion: "What was Apple's total revenue in Q3 2022?",
    // The bounce boundary is a handful of paced deltas wide, so it is
    // sampled far tighter than the 1s stream poll.
    bouncePollIntervalMs: 120,
    bouncePollCount: 400,
    ...options,
  };

  const checks = [];
  let scrollLifecycleCheck = null;
  const check = async (id, body) => {
    const startedAt = Date.now();
    try {
      const outcome = await body();
      checks.push({
        id,
        pass: outcome.pass === true,
        detail: outcome.detail ?? {},
        ms: Date.now() - startedAt,
      });
    } catch (error) {
      const message = error && error.message ? error.message : String(error);
      checks.push({
        id,
        pass: false,
        detail: {},
        error: message.split("\n")[0].slice(0, 300),
        ms: Date.now() - startedAt,
      });
    }
  };

  const readAssistantText = async () => {
    const card = page.locator("article").last().locator(".bg-card");
    const answer = card.locator("p.text-sm.whitespace-pre-wrap").first();
    if ((await answer.count()) === 0) return "";
    const text = (await answer.textContent()) ?? "";
    return text.replace(/\s+/g, " ").trim();
  };

  const readAssistantLen = async () => (await readAssistantText()).length;

  // T2 chrome (specs/workbench-chrome.md): the watchlist + its
  // `+ 关注` submit moved off the sidebar onto /monitor, so the
  // thread page renders one `type="submit"` (this one). The harness
  // still scopes by test id rather than a page-wide submit locator
  // so a future surface that adds another form cannot silently
  // break the lane again.
  const sendButton = () => page.locator('[data-testid="composer-send"]');

  // `waitForURL` returns the moment the address bar changes, but the
  // SPA still shows the PREVIOUS thread's composer until `openThread`
  // resolves for the new id — at which point ThreadPage swaps in the
  // loading placeholder and then a fresh Composer with an empty
  // draft. Filling on the URL alone therefore types into a textarea
  // that is about to be unmounted, and the send button stays disabled
  // on `!draft.trim()`. Refill until the draft sticks and the button
  // arms; that is the composer being ready, with no sleep guessing.
  const fillAndArm = async (question) => {
    const textarea = page.locator("textarea").first();
    const send = sendButton();
    const deadline = Date.now() + cfg.navTimeoutMs;
    let attempts = 0;
    while (Date.now() < deadline) {
      attempts += 1;
      await textarea.fill(question);
      await page.waitForTimeout(150);
      if (
        (await textarea.inputValue()) === question &&
        (await send.isEnabled())
      ) {
        return attempts;
      }
    }
    throw new Error(
      `composer never armed after ${attempts} fills (draft kept being cleared)`,
    );
  };

  // The composer renders this node when the send itself fails (409 / 503 /
  // transport / provider). Polling the full budget after that would only
  // stall the lane, so the stream check ends early and reports the copy.
  const readSendFailure = async () => {
    const node = page.locator('[data-testid="send-failure"]');
    if ((await node.count()) === 0) return "";
    return ((await node.first().textContent()) ?? "").replace(/\s+/g, " ").trim();
  };

  const activeScrollBox = () =>
    page
      .locator("main")
      .last()
      .locator("div.flex-1.overflow-y-auto")
      .first();
  const scrollMetrics = (box) =>
    box.evaluate((el) => ({
      clientHeight: el.clientHeight,
      scrollHeight: el.scrollHeight,
      scrollTop: el.scrollTop,
      bottomGap: Math.max(0, el.scrollHeight - el.clientHeight - el.scrollTop),
    }));
  const sameScrollMetrics = (left, right) =>
    !!left &&
    !!right &&
    left.clientHeight === right.clientHeight &&
    left.scrollHeight === right.scrollHeight &&
    left.scrollTop === right.scrollTop;
  const scrollToBottom = async (box) => {
    await box.hover();
    await page.mouse.wheel(0, 10_000);
    let metrics = await scrollMetrics(box);
    for (let i = 0; i < 10 && metrics.bottomGap > 2; i++) {
      await page.waitForTimeout(100);
      metrics = await scrollMetrics(box);
    }
    return metrics;
  };
  const scrollUpForReading = async (box) => {
    await box.hover();
    await page.mouse.wheel(0, -10_000);
    let metrics = await scrollMetrics(box);
    for (let i = 0; i < 10 && metrics.bottomGap <= 10; i++) {
      await page.waitForTimeout(100);
      metrics = await scrollMetrics(box);
    }
    return metrics;
  };
  const nextAssistantGrowth = async (box, previousLength, doneCountBefore) => {
    for (let i = 0; i < cfg.pollCount; i++) {
      await page.waitForTimeout(Math.min(cfg.pollIntervalMs, 100));
      const [length, metrics, doneCount] = await Promise.all([
        readAssistantLen(),
        scrollMetrics(box),
        page.getByText("完成").count(),
      ]);
      const completed = doneCount > doneCountBefore;
      if (length > previousLength || completed) {
        return { length, metrics, completed };
      }
    }
    return null;
  };
  const waitForMoreArticles = async (previousCount) => {
    const deadline = Date.now() + cfg.navTimeoutMs;
    let count = await page.locator("article").count();
    while (count <= previousCount && Date.now() < deadline) {
      await page.waitForTimeout(100);
      count = await page.locator("article").count();
    }
    return count > previousCount;
  };
  const openNewThread = async (previousRoute) => {
    await page.getByRole("button", { name: "+ 新建 thread" }).click();
    await page.waitForURL((url) => url.toString() !== previousRoute, {
      timeout: cfg.navTimeoutMs,
    });
    return page.url();
  };
  const readThreadInFlight = async (threadId) =>
    page.evaluate(async ({ baseUrl, id }) => {
      const response = await fetch(
        `${baseUrl}/api/threads/${encodeURIComponent(id)}`,
      );
      if (!response.ok) return null;
      const thread = await response.json();
      return thread.in_flight === true;
    }, { baseUrl: cfg.baseUrl, id: threadId });

  // specs/workbench-retrieve-product-seam.md SC6/SC7 retired the whole
  // corpus surface: ``/kb`` + its card grid + the ingest control left
  // the product path ("KnowledgeBasePage / MonitorPage / freeze corpus
  // cards / fin upload shell from the product path"), and deep links
  // to them must 404 — deliberately NOT a redirect home. This check
  // pins the current home chrome (the SPA boot + sidebar that every
  // later check drives) and the retired-route 404. The former
  // ``corpusCards >= 12`` / ``ingestVisible > 0`` assertions flipped to
  // absence assertions: the grid existing again on a product route is
  // exactly what the spec forbids.
  await check("1-home-chrome", async () => {
    await page.goto(cfg.baseUrl + "/", { timeout: cfg.navTimeoutMs });
    const homeHeadingVisible = await page
      .getByText("研究对话")
      .first()
      .isVisible()
      .catch(() => false);
    const newThreadVisible = await page
      .getByRole("button", { name: "+ 新建 thread" })
      .isVisible();
    const mcpLineCount = await page
      .locator('[data-testid="knowledge-mcp-line"]')
      .count();
    const bindLineCount = await page
      .locator('[data-testid="bound-collection-line"]')
      .count();
    const corpusCards = await page
      .locator("button")
      .filter({ hasText: "pages" })
      .count();
    const ingestVisible = await page
      .locator('[data-testid="ingest-control"]')
      .count();

    // The retired deep links must render the plain 404 and keep the
    // address bar (no silent bounce to ``/``). Pathname is sliced off
    // the known baseUrl, not parsed with ``URL``: the snippet runs in
    // the driver context, where the global ``URL`` is not defined.
    const pathname = () => page.url().slice(cfg.baseUrl.length);
    await page.goto(cfg.baseUrl + "/kb", { timeout: cfg.navTimeoutMs });
    const kbNotFound =
      (await page.getByText("页面不存在").count()) === 1 &&
      pathname() === "/kb";
    await page.goto(cfg.baseUrl + "/monitor", { timeout: cfg.navTimeoutMs });
    const monitorNotFound =
      (await page.getByText("页面不存在").count()) === 1 &&
      pathname() === "/monitor";

    // Leave the SPA on the home route for the stream checks.
    await page.goto(cfg.baseUrl + "/", { timeout: cfg.navTimeoutMs });

    return {
      pass:
        homeHeadingVisible &&
        newThreadVisible &&
        mcpLineCount === 1 &&
        bindLineCount === 1 &&
        corpusCards === 0 &&
        ingestVisible === 0 &&
        kbNotFound &&
        monitorNotFound,
      detail: {
        homeHeadingVisible,
        newThreadVisible,
        mcpLineCount,
        bindLineCount,
        corpusCards,
        ingestVisible,
        kbNotFound,
        monitorNotFound,
      },
    };
  });

  await check("2-stream-no-retract", async () => {
    await page.getByRole("button", { name: "+ 新建 thread" }).click();
    await page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs });

    await fillAndArm(cfg.question);
    await sendButton().click();

    let maxLen = 0;
    let sawRetract = false;
    let firstNonZeroAt = -1;
    let firstDoneAt = -1;
    let finalLen = 0;
    let sawDone = false;
    let sendFailure = "";
    const growth = [];

    for (let i = 0; i < cfg.pollCount; i++) {
      await page.waitForTimeout(cfg.pollIntervalMs);
      const len = await readAssistantLen();
      if (len > 0 && firstNonZeroAt < 0) firstNonZeroAt = i;
      if (len > maxLen) maxLen = len;
      if (firstNonZeroAt >= 0 && len === 0) sawRetract = true;
      if (len > 0 && growth[growth.length - 1] !== len) growth.push(len);
      finalLen = len;
      sawDone = (await page.getByText("完成").count()) > 0;
      if (sawDone && firstDoneAt < 0) firstDoneAt = i;
      if (sawDone && len > 20) break;
      sendFailure = await readSendFailure();
      if (sendFailure) break;
    }

    // Distinct growing lengths are the DOM-visible proof that answer
    // deltas reached the live bubble instead of one post-hoc dump.
    const streamedInSteps = growth.length >= 2;
    const answerBeforeDone = firstNonZeroAt >= 0 && firstNonZeroAt < firstDoneAt;

    return {
      pass:
        firstNonZeroAt >= 0 &&
        !sawRetract &&
        finalLen > 0 &&
        (!cfg.requireStreamGrowth || (streamedInSteps && answerBeforeDone)),
      detail: {
        firstNonZeroAt,
        firstDoneAt,
        maxLen,
        finalLen,
        sawRetract,
        sawDone,
        sendFailure,
        growthSteps: growth.length,
        streamedInSteps,
        answerBeforeDone,
        requireStreamGrowth: cfg.requireStreamGrowth,
      },
    };
  });

  // The paced lane samples actual streamed growth, so these checks can
  // distinguish following from a transcript that arrives all at once.
  if (cfg.requireStreamGrowth) {
    const recordScrollGrowth = (result, sample) => {
      const metrics = sample.metrics;
      result.latestLength = sample.length;
      result.growthSteps += 1;
      result.maxScrollHeight = Math.max(result.maxScrollHeight, metrics.scrollHeight);
      result.maxClientHeight = Math.max(result.maxClientHeight, metrics.clientHeight);
      const phase = !result.paused
        ? "bottom"
        : result.reengaged
          ? "reengaged"
          : "reading-older";
      result.growthSamples.push({
        growth: result.growthSteps,
        answerLength: sample.length,
        ...metrics,
        phase,
      });
      if (metrics.scrollHeight <= metrics.clientHeight + 20) return;
      if (!result.paused) {
        result.bottomSamples += 1;
        if (metrics.bottomGap > 3) result.bottomFollowFailures += 1;
      } else if (!result.reengaged) {
        result.pausedGrowths += 1;
        if (Math.abs(metrics.scrollTop - result.pausedAtTop) > 3) {
          result.pausedScrollFailures += 1;
        }
      } else {
        result.reengagedGrowths += 1;
        if (metrics.bottomGap > 3) result.reengagedFollowFailures += 1;
      }
    };
    const transitionScrollPhase = async (result, box) => {
      if (result.growthSteps === 2) {
        await scrollToBottom(box);
        const reading = await scrollUpForReading(box);
        result.pausedAtLength = result.latestLength;
        result.pausedAtTop = reading.scrollTop;
        result.pausedAtGap = reading.bottomGap;
        result.paused = reading.bottomGap > 10;
      }
      if (result.growthSteps === 3) {
        const resumed = await scrollToBottom(box);
        result.reengaged = resumed.bottomGap <= 2;
        result.reengagedAtGrowth = result.growthSteps;
      }
    };
    const collectScrollFollow = async (box, initial, doneCountBefore) => {
      const result = {
        latestLength: 0,
        growthSteps: 0,
        bottomSamples: 0,
        bottomFollowFailures: 0,
        paused: false,
        pausedAtLength: 0,
        pausedAtTop: 0,
        pausedAtGap: 0,
        pausedGrowths: 0,
        pausedScrollFailures: 0,
        reengaged: false,
        reengagedAtGrowth: 0,
        reengagedGrowths: 0,
        reengagedFollowFailures: 0,
        maxScrollHeight: initial.scrollHeight,
        maxClientHeight: initial.clientHeight,
        streamCompleted: false,
        growthSamples: [],
      };
      for (let i = 0; i < cfg.pollCount; i++) {
        const sample = await nextAssistantGrowth(
          box,
          result.latestLength,
          doneCountBefore,
        );
        if (!sample) break;
        if (sample.length > result.latestLength) {
          recordScrollGrowth(result, sample);
          await transitionScrollPhase(result, box);
        }
        if (sample.completed) {
          result.streamCompleted = result.latestLength > 0;
          break;
        }
      }
      return result;
    };
    const scrollFollowVerdict = (initial, articleAppeared, result, errorCount) => {
      const actualOverflow =
        result.maxScrollHeight > result.maxClientHeight + 20;
      const invariants = {
        actualOverflow,
        assistantMessageAppeared: articleAppeared,
        wasAtBottomBeforeSend: initial.bottomGap <= 2,
        observedMultipleGrowthSteps: result.growthSteps >= 4,
        stayedAtBottomDuringGrowth:
          result.bottomSamples >= 2 && result.bottomFollowFailures === 0,
        manualScrollMovedAwayFromBottom:
          result.paused && result.pausedAtGap > 10,
        stayedAtReadingPositionDuringGrowth:
          result.pausedGrowths >= 1 && result.pausedScrollFailures === 0,
        reengagedAtBottom: result.reengaged,
        followedAfterReengaging:
          result.reengagedGrowths >= 1 &&
          result.reengagedFollowFailures === 0,
        streamCompleted: result.streamCompleted,
        noPageErrors: errorCount === 0,
      };
      return {
        pass: Object.values(invariants).every(Boolean),
        detail: {
          invariants,
          initial,
          maxScrollHeight: result.maxScrollHeight,
          maxClientHeight: result.maxClientHeight,
          growthSteps: result.growthSteps,
          bottomSamples: result.bottomSamples,
          bottomFollowFailures: result.bottomFollowFailures,
          pausedAtLength: result.pausedAtLength,
          pausedAtTop: result.pausedAtTop,
          pausedAtGap: result.pausedAtGap,
          pausedGrowths: result.pausedGrowths,
          pausedScrollFailures: result.pausedScrollFailures,
          reengagedAtGrowth: result.reengagedAtGrowth,
          reengagedGrowths: result.reengagedGrowths,
          reengagedFollowFailures: result.reengagedFollowFailures,
          growthSamples: result.growthSamples,
          pageErrors: errorCount,
        },
      };
    };
    const recordLifecycleGrowth = (state, sample) => {
      if (sample.length <= state.latestLength) return;
      state.latestLength = sample.length;
      state.growthSteps += 1;
      const { scrollHeight, clientHeight, bottomGap } = sample.metrics;
      state.maxScrollHeight = Math.max(state.maxScrollHeight, scrollHeight);
      state.maxClientHeight = Math.max(state.maxClientHeight, clientHeight);
      if (scrollHeight <= clientHeight + 20) return;
      state.overflowSamples += 1;
      if (bottomGap > 3) state.followFailures += 1;
    };

    const startLifecycleStream = async (route) => {
      if (!route) throw new Error("page.url() is required for scroll lifecycle coverage");
      await page.setViewportSize({ width: 1280, height: 320 });
      await page.goto(route, { timeout: cfg.navTimeoutMs });
      await page.waitForSelector("article", { timeout: cfg.navTimeoutMs });
      const previousRoute = page.url();
      const previousBox = activeScrollBox();
      await scrollToBottom(previousBox);
      const reading = await scrollUpForReading(previousBox);
      const streamingRoute = await openNewThread(previousRoute);
      const threadId = streamingRoute.slice(`${cfg.baseUrl}/t/`.length);
      await fillAndArm(cfg.question);
      const box = activeScrollBox();
      const emptyThread = await scrollMetrics(box);
      const articleCountBefore = await page.locator("article").count();
      const doneCountBefore = await page.getByText("完成").count();
      await sendButton().click();
      const articleAppeared = await waitForMoreArticles(articleCountBefore);
      const state = {
        latestLength: 0,
        growthSteps: 0,
        overflowSamples: 0,
        followFailures: 0,
        maxScrollHeight: emptyThread.scrollHeight,
        maxClientHeight: emptyThread.clientHeight,
      };
      let inFlight = null;
      for (let i = 0; i < cfg.pollCount; i++) {
        const sample = await nextAssistantGrowth(
          box,
          state.latestLength,
          doneCountBefore,
        );
        if (!sample) break;
        recordLifecycleGrowth(state, sample);
        inFlight = await readThreadInFlight(threadId);
        if (
          state.growthSteps >= 2 &&
          state.overflowSamples >= 2 &&
          inFlight === true
        ) break;
        if (sample.completed) break;
      }
      return {
        previousThreadPaused: reading.bottomGap > 10,
        emptyThread,
        streamingRoute,
        threadId,
        box,
        articleAppeared,
        ...state,
        pendingAtResize: state.growthSteps >= 2 && inFlight === true,
      };
    };

    const resizeAndNavigateLifecycleStream = async (stream) => {
      const viewportBeforeResize = await scrollMetrics(stream.box);
      const oldScroller = await stream.box.elementHandle();
      await page.setViewportSize({ width: 1280, height: 400 });
      const pendingAfterResize = await readThreadInFlight(stream.threadId);
      const viewportAtResize = await oldScroller.evaluate((el) => ({
        connected: el.isConnected,
        clientHeight: el.clientHeight,
        scrollHeight: el.scrollHeight,
        scrollTop: el.scrollTop,
      }));
      const replacementRoute = await openNewThread(stream.streamingRoute);
      await fillAndArm(cfg.question);
      const replacementBox = activeScrollBox();
      const replacementInitial = await scrollMetrics(replacementBox);
      const detachedBaseline = await oldScroller.evaluate((el) => ({
        connected: el.isConnected,
        scrollTop: el.scrollTop,
      }));
      let detachedLatest = detachedBaseline;
      let replacementCurrent = replacementInitial;
      let oldStreamSettled = false;
      let replacementChanged = false;
      for (let i = 0; i < cfg.pollCount; i++) {
        const [inFlight, detached, replacement] = await Promise.all([
          readThreadInFlight(stream.threadId),
          oldScroller.evaluate((el) => ({
            connected: el.isConnected,
            scrollTop: el.scrollTop,
          })),
          scrollMetrics(replacementBox),
        ]);
        detachedLatest = detached;
        replacementCurrent = replacement;
        if (!sameScrollMetrics(replacementInitial, replacement)) {
          replacementChanged = true;
        }
        if (inFlight === false) {
          oldStreamSettled = true;
          break;
        }
        await page.waitForTimeout(Math.min(cfg.pollIntervalMs, 100));
      }
      return {
        viewportBeforeResize,
        viewportAtResize,
        pendingAfterResize,
        replacementRoute,
        replacementInitial,
        replacementCurrent,
        replacementChanged,
        detachedBaseline,
        detachedLatest,
        oldStreamSettled,
      };
    };
    const lifecycleVerdict = (stream, cleanup, errorCount) => {
      const detachedScrollStable =
        cleanup.detachedBaseline.connected === false &&
        cleanup.detachedLatest.connected === false &&
        cleanup.detachedBaseline.scrollTop === cleanup.detachedLatest.scrollTop;
      const invariants = {
        previousThreadWasDeliberatelyPaused: stream.previousThreadPaused,
        freshThreadStartedAtBottom: stream.emptyThread.bottomGap <= 2,
        assistantMessageAppeared: stream.articleAppeared,
        freshThreadHadActualOverflow:
          stream.maxScrollHeight > stream.maxClientHeight + 20,
        freshThreadObservedOngoingGrowth: stream.growthSteps >= 2,
        freshThreadFollowedWhileAtBottom:
          stream.overflowSamples >= 2 && stream.followFailures === 0,
        resizedMountedScrollerWhileStreamWasPending:
          stream.pendingAtResize &&
          cleanup.pendingAfterResize &&
          cleanup.viewportAtResize.connected &&
          cleanup.viewportAtResize.clientHeight !==
            cleanup.viewportBeforeResize.clientHeight,
        navigatedWhileStreamWasPending:
          stream.pendingAtResize &&
          cleanup.replacementRoute !== stream.streamingRoute,
        detachedScrollerStayedAtPostDetachPosition: detachedScrollStable,
        replacementViewportWasNotMutated:
          !cleanup.replacementChanged &&
          sameScrollMetrics(cleanup.replacementInitial, cleanup.replacementCurrent),
        oldStreamSettledAfterNavigation: cleanup.oldStreamSettled,
        noPageErrors: errorCount === 0,
      };
      return {
        pass: Object.values(invariants).every(Boolean),
        detail: {
          invariants,
          emptyThread: stream.emptyThread,
          growthSteps: stream.growthSteps,
          overflowSamples: stream.overflowSamples,
          followFailures: stream.followFailures,
          pendingAtResize: stream.pendingAtResize,
          viewportBeforeResize: cleanup.viewportBeforeResize,
          pendingAfterResize: cleanup.pendingAfterResize,
          viewportAtResize: cleanup.viewportAtResize,
          detachedBaseline: cleanup.detachedBaseline,
          detachedLatest: cleanup.detachedLatest,
          replacementInitial: cleanup.replacementInitial,
          replacementCurrent: cleanup.replacementCurrent,
          replacementChanged: cleanup.replacementChanged,
          oldStreamSettled: cleanup.oldStreamSettled,
          pageErrors: errorCount,
        },
      };
    };

    await check("7-stream-scroll-follow", async () => {
      const originalWidth = 1280;
      const originalHeight = 720;
      const pageErrors = [];
      const onPageError = () => pageErrors.push(true);
      page.on("pageerror", onPageError);
      try {
        await page.setViewportSize({ width: originalWidth, height: 320 });
        const box = activeScrollBox();
        const initial = await scrollToBottom(box);
        const articleCountBefore = await page.locator("article").count();
        const doneCountBefore = await page.getByText("完成").count();
        await fillAndArm(cfg.question);
        await sendButton().click();
        const articleAppeared = await waitForMoreArticles(articleCountBefore);
        const result = await collectScrollFollow(box, initial, doneCountBefore);
        return scrollFollowVerdict(initial, articleAppeared, result, pageErrors.length);
      } finally {
        page.off("pageerror", onPageError);
        await page.setViewportSize({ width: originalWidth, height: originalHeight });
      }
    });

    const lifecycleRoute =
      typeof page.url === "function" ? page.url() : null;
    scrollLifecycleCheck = async () => {
      const pageErrors = [];
      const onPageError = () => pageErrors.push(true);
      page.on("pageerror", onPageError);
      try {
        const stream = await startLifecycleStream(lifecycleRoute);
        const cleanup = await resizeAndNavigateLifecycleStream(stream);
        return lifecycleVerdict(stream, cleanup, pageErrors.length);
      } finally {
        page.off("pageerror", onPageError);
        await page.setViewportSize({ width: 1280, height: 720 });
      }
    };
  }

  await check("4-dom-separation", async () => {
    const domSeparation = await page
      .locator("article")
      .last()
      .evaluate((article) => {
        const card =
          article.querySelector('[data-testid="turn-answer-card"]') ||
          article.querySelector(".bg-card");
        if (!card) {
          return {
            hasCard: false,
            hasTrace: false,
            traceOutsideCard: false,
            answerInsideCard: false,
          };
        }

        const traceByTestId = article.querySelector('[data-testid="turn-trace"]');
        const traceNodes = [];
        if (traceByTestId) {
          traceNodes.push(traceByTestId);
        } else {
          for (const details of article.querySelectorAll("details.bg-tool")) {
            traceNodes.push(details);
          }
          const thinkingButton = Array.from(article.querySelectorAll("button")).find(
            (btn) => /thinking/i.test(btn.textContent ?? ""),
          );
          if (thinkingButton) {
            const thinkingWrap = thinkingButton.closest("div");
            if (thinkingWrap) traceNodes.push(thinkingWrap);
          }
        }

        const uniqueTraces = [...new Set(traceNodes)];
        const hasTrace = uniqueTraces.length > 0;
        const traceOutsideCard =
          !hasTrace || uniqueTraces.every((node) => !card.contains(node));

        const answer =
          card.querySelector("p.text-sm.whitespace-pre-wrap") ||
          card.querySelector("p.text-sm");
        const answerText = (answer?.textContent ?? "").replace(/\s+/g, " ").trim();

        return {
          hasCard: true,
          hasTrace,
          traceCount: uniqueTraces.length,
          traceOutsideCard,
          answerInsideCard: answerText.length > 0,
          answerLen: answerText.length,
          usedTestIds: {
            trace: !!traceByTestId,
            card: !!article.querySelector('[data-testid="turn-answer-card"]'),
          },
        };
      });

    return {
      pass:
        domSeparation.hasCard &&
        domSeparation.answerInsideCard &&
        domSeparation.traceOutsideCard,
      detail: domSeparation,
    };
  });

  // turn-trace-folds (the tracked change / the tracked change): sibling thinking/tool bursts,
  // each collapsed by default once the run ends. Collapsed thinking
  // is header-only (深度思考; no tail preview, no full text);
  // collapsed tools shows the 工具 header (no rows). Expanding
  // brings the content back; a manual toggle round-trips both folds.
  // A turn may hold several thinking bursts; every one of them
  // obeys the same contract.
  await check("5-trace-collapse", async () => {
    const article = page.locator("article").last();

    const thinkingToggle = article
      .locator('[data-testid="trace-thinking-toggle"]')
      .first();
    const toolsToggle = article
      .locator('[data-testid="trace-tools-toggle"]')
      .first();

    const readTrace = () =>
      page
        .locator("article")
        .last()
        .evaluate((el) => {
          const trace = el.querySelector('[data-testid="turn-trace"]');
          const all = (id) =>
            trace
              ? Array.from(trace.querySelectorAll(`[data-testid="${id}"]`))
              : [];
          return {
            hasTrace: !!trace,
            thinkingToggles: all("trace-thinking-toggle").length,
            previews: all("trace-thinking-preview").length,
            fulls: all("trace-thinking-full").length,
            toolsToggle: all("trace-tools-toggle").length,
            toolsListsOpen: all("trace-tools-list").length,
          };
        });

    const collapsed = await readTrace();
    if (
      !collapsed.hasTrace ||
      collapsed.thinkingToggles === 0 ||
      collapsed.toolsToggle === 0
    ) {
      return { pass: false, detail: collapsed };
    }

    // Expand the first thinking fold: its full burst text must come
    // back, and toggling again must re-collapse it (manual override
    // works). Unrelated bursts stay collapsed throughout.
    await thinkingToggle.click();
    const expanded = await readTrace();
    await thinkingToggle.click();
    const recollapsed = await readTrace();

    // Expand the tools fold: the call rows come back as name: summary.
    await toolsToggle.click();
    const toolsOpen = await readTrace();

    return {
      pass:
        collapsed.fulls === 0 &&
        collapsed.toolsListsOpen === 0 &&
        expanded.fulls === 1 &&
        recollapsed.fulls === 0 &&
        toolsOpen.toolsListsOpen === 1,
      detail: {
        thinkingToggles: collapsed.thinkingToggles,
        collapsedPreviews: collapsed.previews,
        collapsedFulls: collapsed.fulls,
        expandedFulls: expanded.fulls,
        recollapsedFulls: recollapsed.fulls,
        toolsListsOpen: toolsOpen.toolsListsOpen,
      },
    };
  });

  // T4: reopening a thread the operator watched stream to `done` runs
  // the GET reconcile through `mergeReconciledTurn`. The observable is
  // not just "prose survives the reload" — it is that the prose never
  // retracts and repaints while the reconcile lands. A single reading
  // after a settle delay cannot see that, so the window is sampled.
  await check("3-reload-persist", async () => {
    const beforeReload = await readAssistantText();
    await page.reload();
    await page.waitForSelector("article", { timeout: cfg.navTimeoutMs });

    const samples = [];
    for (let i = 0; i < 40; i++) {
      samples.push(await readAssistantText());
      await page.waitForTimeout(100);
    }
    const afterReload = samples[samples.length - 1] ?? "";

    // Samples before the first paint are the page loading, not a
    // repaint; the invariant starts once prose is on screen.
    const painted = samples.findIndex((text) => text.length > 0);
    const watched = painted < 0 ? [] : samples.slice(painted);
    const blankedAfterPaint = watched.some((text) => text.length === 0);
    let shrankAfterPaint = false;
    for (let i = 1; i < watched.length; i++) {
      if (watched[i].length < watched[i - 1].length) shrankAfterPaint = true;
    }

    return {
      pass:
        afterReload.length > 0 &&
        afterReload === beforeReload &&
        !blankedAfterPaint &&
        !shrankAfterPaint,
      detail: {
        beforeReload: beforeReload.length,
        afterReload: afterReload.length,
        textIdentical: afterReload === beforeReload,
        paintedAtSample: painted,
        blankedAfterPaint,
        shrankAfterPaint,
        samples: samples.length,
      },
    };
  });

  // specs/workbench-retrieve-product-seam.md retired BOTH status
  // surfaces this check used to drive: the sidebar footer strip
  // (``status-retrieve``) and the /monitor panel (headline / detail /
  // 重连). The host retired the two-state ``retrieve_reachability``
  // field with them — ``GET /api/status`` now publishes the five-field
  // boot snapshot (``knowledge_mcp`` three-state,
  // ``retrieve_adapter_kind``, ``model_identity``,
  // ``corpus_filing_count``, ``bound_collection_name``) and the SPA
  // REJECTS any payload still carrying the old field
  // (``web/src/knowledgeMcp.ts:parseWorkspaceStatus``). The operator
  // surface is the sidebar's two boot-scoped lines on every route.
  // The old invariants map over unchanged in kind:
  //   - "headline agrees with the route" -> the 知识 MCP line's
  //     state/copy must equal the snapshot's ``knowledge_mcp`` (缺/坏 →
  //     「知识 MCP 故障」; 可达 → 「知识 MCP 已连接」, exactly, not
  //     substring);
  //   - "a healthy vs unreachable boot reads distinctly" -> kept, the
  //     verdict is still derived from the route value, never hardcoded;
  //   - "detail text non-empty" -> the sibling bind line must name the
  //     route's ``bound_collection_name`` (or the 未绑定 copy);
  //   - the 重连 control is gone by spec, so its presence assertion
  //     flips to absence on every chrome surface, together with the
  //     legacy strip and panel selectors;
  //   - a line stuck in 检查中 is never a verdict — the poll timeout
  //     reports it, same as the old panel-loading rule.
  // Copy strings are hand-mirrored from ``web/src/knowledgeMcp.ts``
  // (the harness drives the product from outside and pins its copy).
  await check("6-boot-status", async () => {
    const route = await page.evaluate(async (base) => {
      const response = await fetch(base + "/api/status");
      return { ok: response.ok, body: await response.json() };
    }, cfg.baseUrl);

    // One predicate row per snapshot field instead of one growing `&&`
    // chain: adding a field to the contract adds a row, not branching.
    const wireContractOk = (b) =>
      [
        !("retrieve_reachability" in b),
        ["missing", "fault", "reachable"].includes(b.knowledge_mcp),
        ["mcp", "fake"].includes(b.retrieve_adapter_kind),
        typeof b.model_identity === "string" && b.model_identity.length > 0,
        typeof b.corpus_filing_count === "number" &&
          Number.isFinite(b.corpus_filing_count),
        typeof b.bound_collection_name === "string" &&
          b.bound_collection_name.length > 0,
      ].every(Boolean);

    // The sidebar copy is a pure function of the snapshot. Every
    // knowledge_mcp state gets an explicit row so a new state must be
    // added to the table rather than silently falling into an else.
    const expectedCopies = (b) => {
      const mcpCopy = {
        missing: "知识 MCP 故障",
        fault: "知识 MCP 故障",
        reachable: "知识 MCP 已连接",
      };
      const bindState =
        b.bound_collection_name === "unbound" ? "unbound" : "bound";
      const bindCopy = {
        unbound: "资料集合：未绑定",
        bound: `资料集合：${b.bound_collection_name}`,
      };
      return {
        mcpText: mcpCopy[b.knowledge_mcp] ?? "知识 MCP 故障",
        bindState,
        bindText: bindCopy[bindState],
      };
    };

    const readLineText = async (selector) =>
      ((await page.locator(selector).first().textContent()) ?? "")
        .replace(/\s+/g, " ")
        .trim();

    // detail must print null, not undefined, for absent wire fields.
    const orNull = (value) => value ?? null;

    const body = route.body ?? {};
    const expected = expectedCopies(body);

    // The lines are boot-scoped and render ``unknown`` until the
    // status fetch lands; poll the state-pinned selectors until they
    // arm (or the nav budget runs out, which reads as a failure).
    const mcpLineSelector = `[data-testid="knowledge-mcp-line"][data-state="${body.knowledge_mcp ?? "unknown"}"]`;
    const bindLineSelector = `[data-testid="bound-collection-line"][data-state="${expected.bindState}"]`;
    const legacySelectors = [
      '[data-testid="status-retrieve"]',
      '[data-testid="monitor-status-panel"]',
      '[data-testid="monitor-reconnect"]',
    ];
    const countLegacy = async () => {
      let total = 0;
      for (const selector of legacySelectors) {
        total += await page.locator(selector).count();
      }
      return total;
    };

    // 1. thread route (check 3 left the browser on /t/{id}).
    await page.waitForSelector(mcpLineSelector, {
      timeout: cfg.navTimeoutMs,
    });
    await page.waitForSelector(bindLineSelector, {
      timeout: cfg.navTimeoutMs,
    });
    const mcpLineText = await readLineText(mcpLineSelector);
    const bindLineText = await readLineText(bindLineSelector);
    const legacyOnThread = await countLegacy();

    // 2. retired deep link: /monitor must render the 404, not the
    // retired panel or its controls.
    await page.goto(cfg.baseUrl + "/monitor", { timeout: cfg.navTimeoutMs });
    const monitorRetired =
      (await page.getByText("页面不存在").count()) === 1 &&
      (await page.locator('[data-testid="monitor-status-panel"]').count()) ===
        0;
    const legacyOnMonitor = await countLegacy();

    // 3. home route: the sidebar lines ride every product route, so
    // the same copy must arm here too (last-thread restore may bounce
    // / to /t/{id}; either way the chrome must be current).
    await page.goto(cfg.baseUrl + "/", { timeout: cfg.navTimeoutMs });
    await page.waitForSelector(mcpLineSelector, {
      timeout: cfg.navTimeoutMs,
    });
    const legacyOnHome = await countLegacy();

    // The pass conjunction as data rows, same reason as the wire
    // contract: each invariant is one named line, none is a branch.
    const invariants = {
      routeOk: route.ok === true,
      wireContractOk: wireContractOk(body),
      mcpCopyMatchesState: mcpLineText === expected.mcpText,
      bindCopyMatchesRoute: bindLineText === expected.bindText,
      monitorRetired,
      legacyOnThread: legacyOnThread === 0,
      legacyOnMonitor: legacyOnMonitor === 0,
      legacyOnHome: legacyOnHome === 0,
    };

    return {
      pass: Object.values(invariants).every(Boolean),
      detail: {
        knowledgeMcp: orNull(body.knowledge_mcp),
        adapter: orNull(body.retrieve_adapter_kind),
        modelIdentity: orNull(body.model_identity),
        corpusFilings: orNull(body.corpus_filing_count),
        boundCollection: orNull(body.bound_collection_name),
        wireContractOk: invariants.wireContractOk,
        mcpLineText,
        bindLineText,
        monitorRetired,
        legacyOnThread,
        legacyOnMonitor,
        legacyOnHome,
      },
    };
  });

  if (scrollLifecycleCheck) {
    await check("8-scroll-follow-lifecycle", scrollLifecycleCheck);
  }

  // T3: the hidden bounce marker arms the generator's pending reset;
  // the retry's first non-empty delta carries `reset: true` in the
  // same frame as its text. `foldLiveAnswer` then accumulates the
  // retry BEHIND the stale draft, so the bubble converges instead of
  // retracting. What must never happen between the bounce and the
  // terminal frame is a shorter — least of all an empty — bubble.
  if (cfg.expectBounce) {
    // Literal, not BOUNCE_CHECK_ID: this function travels to the
    // browser driver as source text, so a module-scope reference here
    // is a ReferenceError at run time (see the header).
    await check("7-bounce-no-blank", async () => {
      // Each interaction carries the nav timeout and its own label:
      // a bare 30s default-timeout stack trace does not say WHICH
      // step of send -> stream the lane could not reach.
      let step = "goto home";
      const at = async (label, body) => {
        step = label;
        try {
          return await body();
        } catch (error) {
          const message = error && error.message ? error.message : String(error);
          throw new Error(`${label}: ${message.split("\n")[0]}`);
        }
      };

      await at("goto home", () =>
        page.goto(cfg.baseUrl + "/", { timeout: cfg.navTimeoutMs }),
      );
      await at("click 新建 thread", () =>
        page
          .getByRole("button", { name: "+ 新建 thread" })
          .click({ timeout: cfg.navTimeoutMs }),
      );
      await at("await thread url", () =>
        page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs }),
      );
      await at("arm composer", () => fillAndArm(cfg.bounceQuestion));
      await at("click 发送", () =>
        sendButton().click({ timeout: cfg.navTimeoutMs }),
      );
      void step;

      const seen = [];
      let sawDone = false;
      let sendFailure = "";
      for (let i = 0; i < cfg.bouncePollCount; i++) {
        const text = await readAssistantText();
        if (seen[seen.length - 1] !== text) seen.push(text);
        sawDone = (await page.getByText("完成").count()) > 0;
        if (sawDone && text.length > 20) break;
        sendFailure = await readSendFailure();
        if (sendFailure) break;
        await page.waitForTimeout(cfg.bouncePollIntervalMs);
      }

      const painted = seen.findIndex((text) => text.length > 0);
      const watched = painted < 0 ? [] : seen.slice(painted);
      const blanked = watched.some((text) => text.length === 0);
      let shrank = false;
      let shrankFrom = 0;
      let shrankTo = 0;
      for (let i = 1; i < watched.length; i++) {
        if (watched[i].length < watched[i - 1].length) {
          shrank = true;
          shrankFrom = watched[i - 1].length;
          shrankTo = watched[i].length;
        }
      }

      const final = seen[seen.length - 1] ?? "";
      // The scripted draft is the only prose in this run that quotes
      // no page, so its presence proves the gate really bounced and
      // its absence at the end proves the retry replaced it.
      const draftMarker = "eighty-three billion";
      const drafted = seen.some((text) => text.includes(draftMarker));

      return {
        pass:
          drafted &&
          sawDone &&
          !blanked &&
          !shrank &&
          final.length > 0 &&
          !final.includes(draftMarker),
        detail: {
          distinctBubbleStates: seen.length,
          sawUngroundedDraft: drafted,
          sawDone,
          blankedAfterPaint: blanked,
          shrankAfterPaint: shrank,
          shrankFrom,
          shrankTo,
          finalLen: final.length,
          finalKeptDraft: final.includes(draftMarker),
          sendFailure,
        },
      };
    });
  }

  return {
    baseUrl: cfg.baseUrl,
    allPass: checks.every((c) => c.pass),
    checks,
  };
}
