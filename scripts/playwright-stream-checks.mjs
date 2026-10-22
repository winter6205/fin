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

// Checks that pin the SHARED chrome every lane's browser lands on, in
// report order. They read route surfaces, not a turn, so they hold for
// every scenario.
//
// ``1-kb-corpus`` was renamed to ``1-home-chrome`` and
// ``6-status-strip`` to ``6-boot-status``: ``specs/workbench-
// retrieve-product-seam.md`` retired the /kb corpus grid and the
// /monitor status panel (see each check's comment), so the old names
// pinned surfaces that no longer exist.
const CHROME_CHECK_IDS = ["1-home-chrome", "6-boot-status"];

// Checks that pin a COMPLETED, ANSWERED turn: the answer bubble growing
// across streamed deltas, its card/trace separation, the folded trace,
// the prose surviving a reload, and the two scroll-follow invariants.
//
// They are declared here, and then SUPPRESSED for a transcript that
// pauses instead of answering (see ``declaredCheckIds``). That is not a
// skip: those checks still run in full on the default ``grounded`` lane
// and on ``bounce``, so the coverage is not lost — it is simply not
// applicable to a lane whose first send never produces a terminal turn.
// Leaving them declared-and-failing would report the lane red for a
// transcript shape it was not written to assert, and weakening their
// assertions to tolerate a pause would delete exactly the evidence they
// exist to produce.
const ANSWER_TURN_CHECK_IDS = [
  "2-stream-no-retract",
  "4-dom-separation",
  "5-trace-collapse",
  "3-reload-persist",
];

// Runs only when the workspace under test replays a transcript that
// bounces the citation gate (`FIN_SCRIPTED_SCENARIO=bounce`). Declared
// conditionally rather than always, because a lane that cannot produce
// a bounce would otherwise report a permanent FAIL for a check it was
// never asked to run.
const BOUNCE_CHECK_ID = "7-bounce-no-blank";
const SCROLL_CHECK_ID = "7-stream-scroll-follow";
const SCROLL_LIFECYCLE_CHECK_ID = "8-scroll-follow-lifecycle";
const DOCUMENT_CHOICE_CHECK_ID = "7-document-choice-pick";

const BASE_CHECK_IDS = [...CHROME_CHECK_IDS, ...ANSWER_TURN_CHECK_IDS];

/** Ids the runner must see a verdict for, given the same options the
 * check function is called with. An id declared here and missing from
 * the report is a failure, never a skip. */
export function declaredCheckIds(options = {}) {
  return [
    ...CHROME_CHECK_IDS,
    // A transcript that PAUSES for a document choice never reaches a
    // terminal turn on its first send, so the answered-turn checks have
    // nothing to read — and the scroll-follow pair, which watch a
    // streaming answer, nothing to follow. The document-choice check
    // covers the answer surface itself for this lane (it asserts the
    // resumed answer renders, reaches the bubble and cites only the
    // picked source).
    ...(options.expectDocumentChoice ? [] : ANSWER_TURN_CHECK_IDS),
    ...(options.expectDocumentChoice
      ? []
      : options.requireStreamGrowth
        ? [SCROLL_CHECK_ID, SCROLL_LIFECYCLE_CHECK_ID]
        : []),
    ...(options.expectBounce ? [BOUNCE_CHECK_ID] : []),
    ...(options.expectDocumentChoice ? [DOCUMENT_CHOICE_CHECK_ID] : []),
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
    // Opt-in: the workspace under test replays a transcript that pauses
    // the run on a grounded document choice
    // (`FIN_SCRIPTED_SCENARIO=document_choice`). Only then can the
    // browser drive send -> choice card -> pick -> resumed answer, the
    // GE-4 sequence.
    expectDocumentChoice: false,
    // The document-choice lane asks its OWN question: the choice is only
    // reachable on the ``grounded`` lane, and the rules tier reads this
    // wording as grounded because it names a company, a metric and a
    // period. A vaguer question would be answered by the clarify leaf
    // and the run would never reach the admission point, so the card
    // would never paint and every assertion below would be vacuous.
    documentChoiceQuestion: "What was Apple's total revenue in Q3 2022?",
    // The document-choice card is a HOST-side wait, not a paced stream:
    // the run pauses on an interrupt and the projection answers a GET.
    // The resumed leg, however, streams real deltas, so the resumed
    // answer is watched on the stream poll.
    documentChoicePollIntervalMs: 150,
    documentChoicePollCount: 300,
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
  const installBrowserSseObserver = async () =>
    page.evaluate(() => {
      window.__finScriptedSseOpenReaders = 0;
      const originalFetch = window.fetch.bind(window);
      window.fetch = async (...args) => {
        const response = await originalFetch(...args);
        if (
          !String(response.headers.get("content-type") ?? "")
            .toLowerCase()
            .includes("text/event-stream") ||
          !response.body
        ) {
          return response;
        }

        const body = response.body;
        const getReader = body.getReader.bind(body);
        Object.defineProperty(body, "getReader", {
          configurable: true,
          value: (...readerArgs) => {
            const reader = getReader(...readerArgs);
            let open = true;
            window.__finScriptedSseOpenReaders += 1;
            const settle = () => {
              if (!open) return;
              open = false;
              window.__finScriptedSseOpenReaders -= 1;
            };
            const read = reader.read.bind(reader);
            reader.read = (...readArgs) =>
              read(...readArgs).then(
                (result) => {
                  if (result.done) settle();
                  return result;
                },
                (error) => {
                  settle();
                  throw error;
                },
              );
            const cancel = reader.cancel.bind(reader);
            reader.cancel = (...cancelArgs) => {
              settle();
              return cancel(...cancelArgs);
            };
            return reader;
          },
        });
        return response;
      };
    });
  const readBrowserSseOpen = async () =>
    page.evaluate(() => window.__finScriptedSseOpenReaders > 0);

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

  // The answered-turn checks (2 / 4 / 5 / 3) and the two scroll-follow
  // checks read a turn that STREAMED TO DONE. A document-choice transcript
  // pauses on its first send instead, so they have nothing to read; they
  // are declared out in ``declaredCheckIds`` for that lane and skipped
  // here. Every other scenario still runs all of them in full.
  const answeredTurnChecks = !cfg.expectDocumentChoice;

  if (answeredTurnChecks) {
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
  }

  // The paced lane samples actual streamed growth, so these checks can
  // distinguish following from a transcript that arrives all at once.
  if (cfg.requireStreamGrowth && answeredTurnChecks) {
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

    const observeLifecycleStream = async (box, state, doneCountBefore, threadId) => {
      let browserStreamOpen = false;
      let backendCompletedBeforeReplay = false;
      for (let i = 0; i < cfg.pollCount; i++) {
        const sample = await nextAssistantGrowth(
          box,
          state.latestLength,
          doneCountBefore,
        );
        if (!sample) break;
        recordLifecycleGrowth(state, sample);
        browserStreamOpen = await readBrowserSseOpen();
        const backendInFlight = await readThreadInFlight(threadId);
        if (browserStreamOpen && backendInFlight === false) {
          backendCompletedBeforeReplay = true;
        }
        if (
          state.growthSteps >= 2 &&
          state.overflowSamples >= 2 &&
          browserStreamOpen
        ) break;
        if (sample.completed) break;
      }
      return {
        browserStreamOpenAtResize: state.growthSteps >= 2 && browserStreamOpen,
        backendCompletedBeforeReplay,
      };
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
      await installBrowserSseObserver();
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
      const streamState = await observeLifecycleStream(
        box,
        state,
        doneCountBefore,
        threadId,
      );
      return {
        previousThreadPaused: reading.bottomGap > 10,
        emptyThread,
        streamingRoute,
        threadId,
        box,
        articleAppeared,
        ...state,
        ...streamState,
      };
    };

    const resizeAndNavigateLifecycleStream = async (stream) => {
      const viewportBeforeResize = await scrollMetrics(stream.box);
      const oldScroller = await stream.box.elementHandle();
      await page.setViewportSize({ width: 1280, height: 400 });
      const browserStreamOpenAfterResize = await readBrowserSseOpen();
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
      let browserStreamSettled = false;
      let replacementChanged = false;
      for (let i = 0; i < cfg.pollCount; i++) {
        const [browserStreamOpen, detached, replacement] = await Promise.all([
          readBrowserSseOpen(),
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
        if (!browserStreamOpen) {
          browserStreamSettled = true;
          break;
        }
        await page.waitForTimeout(Math.min(cfg.pollIntervalMs, 100));
      }
      return {
        viewportBeforeResize,
        viewportAtResize,
        browserStreamOpenAfterResize,
        replacementRoute,
        replacementInitial,
        replacementCurrent,
        replacementChanged,
        detachedBaseline,
        detachedLatest,
        browserStreamSettled,
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
        backendCompletedBeforeBrowserReplay:
          stream.backendCompletedBeforeReplay,
        resizedMountedScrollerWhileStreamWasPending:
          stream.browserStreamOpenAtResize &&
          cleanup.browserStreamOpenAfterResize &&
          cleanup.viewportAtResize.connected &&
          cleanup.viewportAtResize.clientHeight !==
            cleanup.viewportBeforeResize.clientHeight,
        navigatedWhileStreamWasPending:
          stream.browserStreamOpenAtResize &&
          cleanup.replacementRoute !== stream.streamingRoute,
        detachedScrollerStayedAtPostDetachPosition: detachedScrollStable,
        replacementViewportWasNotMutated:
          !cleanup.replacementChanged &&
          sameScrollMetrics(cleanup.replacementInitial, cleanup.replacementCurrent),
        browserStreamSettledAfterNavigation: cleanup.browserStreamSettled,
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
          browserStreamOpenAtResize: stream.browserStreamOpenAtResize,
          backendCompletedBeforeReplay: stream.backendCompletedBeforeReplay,
          viewportBeforeResize: cleanup.viewportBeforeResize,
          browserStreamOpenAfterResize: cleanup.browserStreamOpenAfterResize,
          viewportAtResize: cleanup.viewportAtResize,
          detachedBaseline: cleanup.detachedBaseline,
          detachedLatest: cleanup.detachedLatest,
          replacementInitial: cleanup.replacementInitial,
          replacementCurrent: cleanup.replacementCurrent,
          replacementChanged: cleanup.replacementChanged,
          browserStreamSettled: cleanup.browserStreamSettled,
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

  if (answeredTurnChecks) {
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

  // turn-trace-folds: sibling thinking/tool bursts,
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
  }

  // T4: reopening a thread the operator watched stream to `done` runs
  // the GET reconcile through `mergeReconciledTurn`. The observable is
  // not just "prose survives the reload" — it is that the prose never
  // retracts and repaints while the reconcile lands. A single reading
  // after a settle delay cannot see that, so the window is sampled.
  if (answeredTurnChecks) {
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
  }

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

  const readFinalBounceRun = async () => {
    const threadId = page.url().slice(`${cfg.baseUrl}/t/`.length);
    return page.evaluate(async ({ baseUrl, id }) => {
      const response = await fetch(
        `${baseUrl}/api/threads/${encodeURIComponent(id)}`,
      );
      if (!response.ok) return null;
      const thread = await response.json();
      return thread.turns?.at(-1) ?? null;
    }, { baseUrl: cfg.baseUrl, id: threadId });
  };
  const summarizeBounceStates = (seen) => {
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
    const draftMarker = "eighty-three billion";
    return {
      blanked,
      shrank,
      shrankFrom,
      shrankTo,
      leakedUngroundedDraft: seen.some((text) => text.includes(draftMarker)),
    };
  };
  const hasSourceReadEvidence = (run) =>
    run?.status === "done" &&
    run.tool_summaries?.some((tool) => tool.name === "read_source") &&
    run.evidence_locators?.some(
      (locator) => locator.document_id === "AAPL-2022Q3",
    );
  const bounceVerdict = (seen, sawDone, sendFailure, finalRun) => {
    const final = seen[seen.length - 1] ?? "";
    const summary = summarizeBounceStates(seen);
    const admittedRetryMarker = "Per AAPL-2022Q3 page";
    const admittedRetryObserved = final.includes(admittedRetryMarker);
    const sourceReadObserved = hasSourceReadEvidence(finalRun);
    const pass =
      !summary.leakedUngroundedDraft &&
      sawDone &&
      !summary.blanked &&
      !summary.shrank &&
      seen.length >= 2 &&
      final.length > 0 &&
      admittedRetryObserved &&
      sourceReadObserved;
    return {
      pass,
      detail: {
        distinctBubbleStates: seen.length,
        leakedUngroundedDraft: summary.leakedUngroundedDraft,
        admittedRetryObserved,
        sourceReadObserved,
        sawDone,
        blankedAfterPaint: summary.blanked,
        shrankAfterPaint: summary.shrank,
        shrankFrom: summary.shrankFrom,
        shrankTo: summary.shrankTo,
        finalLen: final.length,
        sendFailure,
      },
    };
  };

  // T3: the hidden bounce marker makes the citation gate reject the
  // first attempt. That unadmitted draft stays private; the browser
  // observes only the source-backed retry answer and its terminal state.
  // The bubble must never paint the rejected prose, blank after paint,
  // or shrink while the admitted answer arrives.
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

      const finalRun = await readFinalBounceRun();
      return bounceVerdict(seen, sawDone, sendFailure, finalRun);
    });
  }

  // T9 / GE-4 (specs/qa-chain-grounded-experience.md:96): the document
  // choice, driven end to end from the browser.
  //
  //   send -> the run pauses -> the card renders its candidates ->
  //   reload (the projection, not the wire, restores it) ->
  //   pick -> the SAME run answers inside the picked source.
  //
  // Declared conditionally for the same reason as the bounce check: a
  // lane whose transcript cannot pause would otherwise report a
  // permanent FAIL for a check it was never asked to run.
  //
  // What is asserted, in GE-4's own words:
  //
  //   * "<=4 hit-backed candidates have title/section/summary and
  //     trailing rephrase" — the rows carry a title and a non-empty
  //     summary, there are 1..4 of them, and exactly one trailing
  //     换个问法 action sits after them;
  //   * "browser RELOAD ... preserves the original run/receipt" — the
  //     card is read again AFTER a reload, from the thread projection,
  //     and still names the same question and the same origin run id.
  //     The question's `origin_run_id` is read from the DOM's own
  //     question id, so the run the operator sees is the run the host
  //     bound the question to;
  //   * "PICK ... preserves the original run/receipt and current source
  //     authority" — after the pick, the thread still names that same
  //     run id and now carries a restricted scope naming exactly the
  //     picked candidate's source;
  //   * "and current source authority" — the resumed answer CITES the
  //     picked candidate and no other, which is the narrowing observable
  //     on the operator's screen rather than only in a state field.
  const documentChoiceAt = async (label, body) => {
    try {
      return await body();
    } catch (error) {
      const message = error && error.message ? error.message : String(error);
      throw new Error(label + ": " + message.split("\n")[0]);
    }
  };

  const documentChoiceCurrentThreadId = () =>
    page.url().slice((cfg.baseUrl + "/t/").length).split(/[?#]/)[0];

  const readDocumentChoiceProjection = () =>
    page.evaluate(
      async ({ baseUrl, threadId }) => {
        const projectChoiceDetails = (question) => {
          if (!question) return [];
          return (question.choice_details || []).map((row) => ({
            choice_id: row.choice_id,
            label: row.label,
            summary: row.summary,
            section: row.source_ref ? row.source_ref.section : null,
            source_id: row.source_ref ? row.source_ref.source_id : null,
          }));
        };
        const projectQuestion = (question) => ({
          question_id: question ? question.question_id : null,
          purpose: question ? question.purpose : null,
          origin_run_id: question ? question.origin_run_id : null,
          prompt: question ? question.prompt : null,
          choices: question ? question.choices : null,
          details: projectChoiceDetails(question),
        });
        const projectLastInput = (taskRuntime) => {
          const history =
            taskRuntime && taskRuntime.input_history
              ? taskRuntime.input_history
              : [];
          return history.length > 0 ? history[history.length - 1] : null;
        };
        const projectTurns = (turns) =>
          (turns || []).map((turn) => ({
            run_id: turn.run_id,
            status: turn.status,
            answer: turn.answer || "",
            citations: (turn.citations || []).map((citation) => citation.document_id),
          }));

        const response = await fetch(
          baseUrl + "/api/threads/" + encodeURIComponent(threadId),
        );
        if (!response.ok) return null;
        const thread = await response.json();
        const taskRuntime = thread.task_runtime || {};
        const question = taskRuntime.pending_question || null;
        return {
          pending_interrupt_kind: thread.pending_interrupt_kind ?? null,
          ...projectQuestion(question),
          scope: thread.source_scope || null,
          input_version: taskRuntime.input_version,
          last_input: projectLastInput(taskRuntime),
          turns: projectTurns(thread.turns),
        };
      },
      { baseUrl: cfg.baseUrl, threadId: documentChoiceCurrentThreadId() },
    );

  const readDocumentChoiceRows = () =>
    page.evaluate(() => {
      const node = document.querySelector(
        '[data-testid="document-choice-card"]',
      );
      if (!node) return [];

      const readText = (prefix, choiceId) => {
        const element = Array.from(node.querySelectorAll("[data-testid]")).find(
          (candidate) =>
            candidate.getAttribute("data-testid") === prefix + choiceId,
        );
        return element ? (element.textContent || "").trim() : null;
      };
      const isChoiceButton = (button) =>
        (button.getAttribute("data-testid") || "").startsWith(
          "document-choice-row-",
        );
      const projectChoiceButton = (button) => {
        const testid = button.getAttribute("data-testid") || "";
        const choiceId = testid.slice("document-choice-row-".length);
        return {
          choice_id: choiceId,
          title: readText("document-choice-title-", choiceId) || "",
          summary: readText("document-choice-summary-", choiceId) || "",
          section: readText("document-choice-section-", choiceId),
        };
      };
      return Array.from(node.querySelectorAll("button"))
        .filter(isChoiceButton)
        .map(projectChoiceButton);
    });

  const documentChoiceCard = () =>
    page.locator('[data-testid="document-choice-card"]');
  const documentChoicePrompt = () =>
    page.locator('[data-testid="document-choice-prompt"]');
  const documentChoiceRephrase = () =>
    page.locator('[data-testid="document-choice-rephrase"]');

  const defaultCopyIsSafe = (text) =>
    !/\b(?:document_id|list_files|10-Q|XBRL)\b/i.test(text) &&
    !/\b[A-Z]{2,6}-\d{4}Q[1-4]\b/.test(text);

  const documentChoiceCardInvariants = ({ rows, paused }) => ({
    everyCandidateHasReadableTitle: rows.every(
      (row) =>
        /Apple Inc\./.test(row.title) &&
        /202[23] Q[1-4]/.test(row.title) &&
        defaultCopyIsSafe(row.title),
    ),
    everyCandidateHasReadableSection: rows.every(
      (row) =>
        /^Item\s+\d+[A-Z]?\.\s+\S/i.test(row.section || "") &&
        defaultCopyIsSafe(row.section || ""),
    ),
    candidateTitlesDistinguishPeriods:
      new Set(rows.map((row) => row.title)).size === rows.length,
    everyCandidateHasInformativeSummary: rows.every(
      (row) =>
        ((/2022 Q3/.test(row.title) &&
          row.summary ===
            "资料中列出了“Products $ 63,355 $ 63,948 $ 245,241 $ 232,309”。") ||
          (/2023 Q3/.test(row.title) &&
            row.summary ===
              "资料中列出了“Products $ 60,584 $ 63,355 $ 230,901 $ 245,241”。")) &&
        defaultCopyIsSafe(row.summary),
    ),
    projectedReadableChoicesMatchDom:
      paused !== null &&
      paused.details.length === rows.length &&
      paused.details.every(
        (row, index) =>
          row.label === rows[index].title &&
          row.summary === rows[index].summary &&
          defaultCopyIsSafe(row.label + row.summary),
      ),
  });

  const documentChoiceAnswerInvariants = ({ turn, answered }) => ({
    answerHasReadableSourceLabel:
      turn !== null &&
      turn.answer.includes("AAPL · 2022 Q3") &&
      answered.includes("AAPL · 2022 Q3"),
    defaultAnswerHasNoInternalOrBoilerplateLabels:
      turn !== null &&
      defaultCopyIsSafe(turn.answer) &&
      defaultCopyIsSafe(answered),
    answerQuotesRelevantSourceRow:
      turn !== null &&
      turn.answer.includes("Total net sales 82,959 81,434 304,182 282,457"),
  });

  const documentChoiceVisibleInvariants = ({
    rows,
    cardPresentBeforePick,
    rephrasePresentBeforePick,
    rephraseText,
    promptText,
  }) => ({
    cardRendered: cardPresentBeforePick,
    candidateCountWithinCap: rows.length >= 1 && rows.length <= 4,
    trailingRephrasePresent:
      rephrasePresentBeforePick && rephraseText.length > 0,
    promptRendered: promptText.length > 0,
  });

  const documentChoicePauseInvariants = ({ rows, paused }) => ({
    pausedOnDocumentChoice:
      paused !== null && paused.purpose === "document_choice",
    pausedInterruptKind:
      paused !== null &&
      paused.pending_interrupt_kind === "document_choice",
    projectedCandidateCountMatchesDom:
      paused !== null &&
      paused.details.length === rows.length &&
      rows.length > 0,
    projectedCandidateIdsMatchDom:
      paused !== null &&
      paused.details.length > 0 &&
      JSON.stringify(paused.details.map((row) => row.choice_id)) ===
        JSON.stringify(rows.map((row) => row.choice_id)),
    everyProjectedCandidateHasSummary:
      paused !== null &&
      paused.details.every((row) => row.summary.length > 0),
  });

  const documentChoiceReloadInvariants = ({
    rows,
    promptText,
    reloadedRows,
    reloadedPrompt,
    afterReload,
    paused,
  }) => ({
    reloadKeptTheCard: reloadedRows.length === rows.length,
    reloadKeptTheQuestion:
      reloadedPrompt === promptText && reloadedPrompt.length > 0,
    reloadKeptTheRun:
      afterReload !== null &&
      paused !== null &&
      afterReload.origin_run_id === paused.origin_run_id &&
      afterReload.question_id === paused.question_id &&
      afterReload.origin_run_id !== null,
  });

  const documentChoicePickInvariants = ({ paused, resumed }) => ({
    pickAcceptedOnce:
      resumed !== null &&
      resumed.last_input !== null &&
      resumed.last_input.kind === "reply",
    pickDidNotAdvanceTwice:
      paused !== null &&
      resumed !== null &&
      typeof resumed.input_version === "number" &&
      resumed.input_version <= paused.input_version + 1,
  });

  const documentChoiceScopeInvariants = ({ rows, paused, resumed }) => ({
    scopeRestrictedToThePickedSource:
      resumed !== null &&
      paused !== null &&
      paused.details.length > 0 &&
      resumed.scope !== null &&
      resumed.scope.mode === "restricted" &&
      resumed.scope.sources.length === 1 &&
      resumed.scope.sources[0].source_id === paused.details[0].source_id,
  });

  const documentChoiceAuthorityInvariants = ({ paused, turn }) => ({
    answerConfinedToThePickedSource:
      turn !== null &&
      paused !== null &&
      paused.details.length > 0 &&
      turn.citations.length > 0 &&
      turn.citations.every(
        (documentId) => documentId === paused.details[0].source_id,
      ),
  });

  const documentChoiceAnswerSurfaceInvariants = ({
    turn,
    answered,
    cardClosedAfterThePick,
  }) => ({
    answerRendered: turn !== null && turn.answer.trim().length > 0,
    answerReachedTheBubble: answered.length > 0,
    cardClosedAfterThePick,
  });

  const documentChoiceFieldOrNull = (source, field) =>
    source ? source[field] : null;

  const documentChoiceFirstSourceId = (paused) => {
    const first = paused && paused.details && paused.details[0];
    return documentChoiceFieldOrNull(first, "source_id");
  };

  const documentChoiceAnswerSnippet = (turn) =>
    turn ? turn.answer.slice(0, 160) : null;

  const documentChoiceDetail = (state) => {
    return {
      invariants: state.invariants,
      rows: state.rows,
      reloadedRows: state.reloadedRows,
      promptText: state.promptText,
      rephraseText: state.rephraseText,
      pausedQuestionId: documentChoiceFieldOrNull(
        state.paused,
        "question_id",
      ),
      pausedOriginRunId: documentChoiceFieldOrNull(
        state.paused,
        "origin_run_id",
      ),
      pickedChoiceId: state.picked.choice_id,
      pickedSourceId: documentChoiceFirstSourceId(state.paused),
      afterReloadQuestionId: documentChoiceFieldOrNull(
        state.afterReload,
        "question_id",
      ),
      afterReloadOriginRunId: documentChoiceFieldOrNull(
        state.afterReload,
        "origin_run_id",
      ),
      resumedRunId: documentChoiceFieldOrNull(state.turn, "run_id"),
      resumedAnswer: documentChoiceAnswerSnippet(state.turn),
      resumedCitations: documentChoiceFieldOrNull(state.turn, "citations"),
      resumedInputVersion: documentChoiceFieldOrNull(
        state.resumed,
        "input_version",
      ),
      resumedScope: documentChoiceFieldOrNull(state.resumed, "scope"),
      bubbleLength: state.answered.length,
    };
  };

  const documentChoiceOutcome = (state) => {
    const invariants = {
      ...documentChoiceVisibleInvariants(state),
      ...documentChoiceCardInvariants(state),
      ...documentChoicePauseInvariants(state),
      ...documentChoiceReloadInvariants(state),
      ...documentChoicePickInvariants(state),
      ...documentChoiceScopeInvariants(state),
      ...documentChoiceAuthorityInvariants(state),
      ...documentChoiceAnswerInvariants(state),
      ...documentChoiceAnswerSurfaceInvariants(state),
    };
    return {
      pass: Object.values(invariants).every(Boolean),
      detail: documentChoiceDetail({ ...state, invariants }),
    };
  };

  const openDocumentChoice = async () => {
    await documentChoiceAt("goto home", () =>
      page.goto(cfg.baseUrl + "/", { timeout: cfg.navTimeoutMs }),
    );
    await documentChoiceAt("click 新建 thread", () =>
      page
        .getByRole("button", { name: "+ 新建 thread" })
        .click({ timeout: cfg.navTimeoutMs }),
    );
    await documentChoiceAt("await thread url", () =>
      page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs }),
    );
    await documentChoiceAt("arm composer", () =>
      fillAndArm(cfg.documentChoiceQuestion),
    );
    await documentChoiceAt("click 发送", () =>
      sendButton().click({ timeout: cfg.navTimeoutMs }),
    );
    await documentChoiceAt("await document choice card", () =>
      page.waitForSelector('[data-testid="document-choice-card"]', {
        timeout: cfg.navTimeoutMs,
      }),
    );

    const rows = await readDocumentChoiceRows();
    const promptText = ((await documentChoicePrompt().textContent()) ?? "")
      .replace(/\s+/g, " ")
      .trim();
    const rephraseText = ((await documentChoiceRephrase().textContent()) ?? "")
      .replace(/\s+/g, " ")
      .trim();
    const cardPresentBeforePick =
      (await documentChoiceCard().count()) === 1;
    const rephrasePresentBeforePick =
      (await documentChoiceRephrase().count()) === 1;
    const paused = await readDocumentChoiceProjection();
    return {
      rows,
      promptText,
      rephraseText,
      cardPresentBeforePick,
      rephrasePresentBeforePick,
      paused,
    };
  };

  const reloadDocumentChoice = async (state) => {
    await documentChoiceAt("reload", () => page.reload());
    await documentChoiceAt("await card after reload", () =>
      page.waitForSelector('[data-testid="document-choice-card"]', {
        timeout: cfg.navTimeoutMs,
      }),
    );
    return {
      reloadedRows: await readDocumentChoiceRows(),
      reloadedPrompt: ((await documentChoicePrompt().textContent()) ?? "")
        .replace(/\s+/g, " ")
        .trim(),
      afterReload: await readDocumentChoiceProjection(),
      promptText: state.promptText,
      rows: state.rows,
    };
  };

  const pickDocumentChoice = async (rows) => {
    const picked = rows[0];
    await documentChoiceAt("click candidate row", () =>
      page
        .locator(
          "[data-testid=\"document-choice-row-" + picked.choice_id + "\"]",
        )
        .click({ timeout: cfg.navTimeoutMs }),
    );
    await documentChoiceAt("click 提交选择", () =>
      page
        .locator('[data-testid="document-choice-submit"]')
        .click({ timeout: cfg.navTimeoutMs }),
    );
    return picked;
  };

  const findDocumentChoiceTurn = (projection, originRunId) => {
    if (!originRunId || !projection) return null;
    return projection.turns.find((row) => row.run_id === originRunId) || null;
  };

  const pollDocumentChoiceCompletion = async (originRunId) => {
    for (let i = 0; i < cfg.documentChoicePollCount; i++) {
      const projection = await readDocumentChoiceProjection();
      const candidate = findDocumentChoiceTurn(projection, originRunId);
      const cardGone = (await documentChoiceCard().count()) === 0;
      if (candidate && candidate.status === "done" && cardGone) {
        return projection;
      }
      await page.waitForTimeout(cfg.documentChoicePollIntervalMs);
    }
    return null;
  };

  const readDocumentChoiceCompletion = async (originRunId) => {
    const polled = await pollDocumentChoiceCompletion(originRunId);
    const resumed = polled || (await readDocumentChoiceProjection());
    const turn = findDocumentChoiceTurn(resumed, originRunId) || null;
    return { resumed, turn };
  };

  const runDocumentChoiceWorkflow = async () => {
    const opened = await openDocumentChoice();
    const reloaded = await reloadDocumentChoice(opened);
    const picked = await pickDocumentChoice(opened.rows);
    const originRunId = opened.paused ? opened.paused.origin_run_id : null;
    const { resumed, turn } =
      await readDocumentChoiceCompletion(originRunId);
    const answered = await readAssistantText();
    const cardClosedAfterThePick =
      (await documentChoiceCard().count()) === 0;
    return documentChoiceOutcome({
      ...opened,
      ...reloaded,
      picked,
      resumed,
      turn,
      answered,
      cardClosedAfterThePick,
    });
  };

  const maybeRunDocumentChoiceCheck = async () => {
    if (!cfg.expectDocumentChoice) return;
    await check("7-document-choice-pick", runDocumentChoiceWorkflow);
  };

  await maybeRunDocumentChoiceCheck();

  return {
    baseUrl: cfg.baseUrl,
    allPass: checks.every((c) => c.pass),
    checks,
  };
}
