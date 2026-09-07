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
const BASE_CHECK_IDS = [
  "1-kb-corpus",
  "2-stream-no-retract",
  "4-dom-separation",
  "5-trace-collapse",
  "3-reload-persist",
  "6-status-strip",
];

// Runs only when the workspace under test replays a transcript that
// bounces the citation gate (`FIN_SCRIPTED_SCENARIO=bounce`). Declared
// conditionally rather than always, because a lane that cannot produce
// a bounce would otherwise report a permanent FAIL for a check it was
// never asked to run.
const BOUNCE_CHECK_ID = "7-bounce-no-blank";

/** Ids the runner must see a verdict for, given the same options the
 * check function is called with. An id declared here and missing from
 * the report is a failure, never a skip. */
export function declaredCheckIds(options = {}) {
  return options.expectBounce
    ? [...BASE_CHECK_IDS, BOUNCE_CHECK_ID]
    : [...BASE_CHECK_IDS];
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

  // T1 workbench chrome: corpus cards + IngestCorpusControl moved off
  // ``/`` onto ``/kb`` (specs/workbench-chrome.md). The check drives
  // ``/kb`` and asserts the catalog surface is intact; ``/`` is the
  // 对话 empty / last-thread restore surface and does not carry the
  // card grid.
  await check("1-kb-corpus", async () => {
    await page.goto(cfg.baseUrl + "/kb", { timeout: cfg.navTimeoutMs });
    await page
      .locator("button")
      .filter({ hasText: "pages" })
      .first()
      .waitFor({ timeout: cfg.navTimeoutMs });
    const corpusCards = await page
      .locator("button")
      .filter({ hasText: "pages" })
      .count();
    const newThreadVisible = await page
      .getByRole("button", { name: "+ 新建 thread" })
      .isVisible();
    const ingestVisible = await page
      .locator('[data-testid="ingest-control"]')
      .count();
    return {
      pass: corpusCards >= 12 && newThreadVisible && ingestVisible > 0,
      detail: { corpusCards, newThreadVisible, ingestVisible },
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

  // turn-trace-folds (#250) replaces the old accordion/details trace:
  // sibling thinking/tool bursts, each collapsed by default once the
  // run ends. Collapsed thinking shows the 深度思考 header + a tail
  // preview (no full text); collapsed tools shows the 工具 header
  // (no rows). Collapsed is not lost: expanding brings the content
  // back, and a manual toggle round-trips both folds. A turn may
  // hold several thinking bursts (pre-tool and post-tool reasoning);
  // every one of them obeys the same contract.
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
        collapsed.previews === collapsed.thinkingToggles &&
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

  // T3 (specs/workbench-chrome.md): the Sidebar footer strip is
  // gone. The status copy now lives on /monitor, and an
  // unreachable retrieve paints a non-text-only mark on the 监控
  // entry. This check asserts the new IA on three surfaces:
  //   1. on /monitor, the panel + 重连 button are present;
  //   2. on a thread route, the strip selector is gone;
  //   3. on the home route, the strip selector is gone.
  // The probe answer is checked against the rendered headline so
  // a healthy vs unreachable boot reads distinctly.
  await check("6-status-strip", async () => {
    const route = await page.evaluate(async (base) => {
      const response = await fetch(base + "/api/status");
      return { ok: response.ok, body: await response.json() };
    }, cfg.baseUrl);

    const unreachable = route.body?.retrieve_reachability === "unreachable";

    // /monitor — the status panel must render with a headline +
    // 重连 control. Headline agrees with the route: unreachable
    // → 不可用, reachable (mcp or fake) → 可用 (excluding the
    // negation, since 不可用 contains 可用). The panel starts in
    // the 读取中 loading state until the status fetch lands, so the
    // headline is polled until it settles (loading never counts as
    // a verdict — a panel stuck loading is what a poll timeout
    // reports).
    await page.goto(cfg.baseUrl + "/monitor", { timeout: cfg.navTimeoutMs });
    const panel = page.locator('[data-testid="monitor-status-panel"]');
    const panelCount = await panel.count();
    let headlineText = "";
    let detailText = "";
    let reconnectCount = 0;
    if (panelCount > 0) {
      const readHeadline = async () =>
        (
          (await panel
            .locator('[data-testid="monitor-status-headline"]')
            .first()
            .textContent()) ?? ""
        ).replace(/\s+/g, " ").trim();
      const deadline = Date.now() + cfg.navTimeoutMs;
      while (Date.now() < deadline) {
        headlineText = await readHeadline();
        if (headlineText.length > 0 && !headlineText.includes("读取中")) break;
        await page.waitForTimeout(100);
      }
      detailText = (
        (await panel
          .locator('[data-testid="monitor-status-detail"]')
          .first()
          .textContent()) ?? ""
      ).replace(/\s+/g, " ").trim();
      reconnectCount = await panel
        .locator('[data-testid="monitor-reconnect"]')
        .count();
    }

    const headlineMatchesRoute = unreachable
      ? headlineText.includes("不可用")
      : headlineText.includes("可用") &&
        !headlineText.includes("不可用");

    // Sidebar — the legacy footer strip selector must be gone on
    // every chrome surface. /monitor is already loaded; we hop
    // through / and /t/{id} to assert each.
    async function legacyStripAbsent(label) {
      await page.goto(cfg.baseUrl + label, { timeout: cfg.navTimeoutMs });
      const legacy = await page
        .locator('[data-testid="status-retrieve"]')
        .count();
      return legacy;
    }
    const onHomeLegacy = await legacyStripAbsent("/");
    // New thread so we have a /t/{id} to verify against.
    await page.goto(cfg.baseUrl + "/monitor", { timeout: cfg.navTimeoutMs });
    await page
      .getByRole("button", { name: "+ 新建 thread" })
      .click({ timeout: cfg.navTimeoutMs });
    await page.waitForURL(/\/t\//, { timeout: cfg.navTimeoutMs });
    const onThreadLegacy = await page
      .locator('[data-testid="status-retrieve"]')
      .count();
    await page.goto(cfg.baseUrl + "/monitor", { timeout: cfg.navTimeoutMs });

    return {
      pass:
        route.ok === true &&
        panelCount === 1 &&
        headlineMatchesRoute &&
        headlineText.length > 0 &&
        detailText.length > 0 &&
        reconnectCount === 1 &&
        onHomeLegacy === 0 &&
        onThreadLegacy === 0,
      detail: {
        reachability: route.body?.retrieve_reachability ?? null,
        adapter: route.body?.retrieve_adapter_kind ?? null,
        headlineText,
        detailLen: detailText.length,
        reconnectCount,
        onHomeLegacy,
        onThreadLegacy,
        headlineMatchesRoute,
      },
    };
  });

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
