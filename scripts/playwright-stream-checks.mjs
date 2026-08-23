// Four playwright-cli checks for streaming answer stability and layout.
module.exports = async (page) => {
  const checks = [];
  const base = "http://127.0.0.1:8000";

  await page.goto(base + "/");
  await page
    .locator("button")
    .filter({ hasText: "pages" })
    .first()
    .waitFor({ timeout: 10_000 });
  const corpusCards = await page
    .locator("button")
    .filter({ hasText: "pages" })
    .count();
  const newThreadVisible = await page
    .getByRole("button", { name: "+ 新建 thread" })
    .isVisible();
  checks.push({
    id: "1-home-corpus",
    pass: corpusCards >= 12 && newThreadVisible,
    detail: { corpusCards, newThreadVisible },
  });

  await page.getByRole("button", { name: "+ 新建 thread" }).click();
  await page.waitForURL(/\/t\//, { timeout: 10_000 });

  const textarea = page.locator("textarea").first();
  await textarea.fill("What was Apple's total revenue in Q3 2022?");
  await page.getByRole("button", { name: "发送" }).click();

  const readAssistantLen = async () => {
    const card = page.locator("article").last().locator(".bg-card");
    const answer = card.locator("p.text-sm.whitespace-pre-wrap").first();
    const count = await answer.count();
    if (count === 0) return 0;
    const text = (await answer.textContent()) ?? "";
    return text.replace(/\s+/g, " ").trim().length;
  };

  let maxLen = 0;
  let sawRetract = false;
  let firstNonZeroAt = -1;
  let finalLen = 0;
  let sawDone = false;

  for (let i = 0; i < 180; i++) {
    await page.waitForTimeout(1000);
    const len = await readAssistantLen();
    if (len > 0 && firstNonZeroAt < 0) firstNonZeroAt = i;
    if (len > maxLen) maxLen = len;
    if (firstNonZeroAt >= 0 && len === 0) sawRetract = true;
    finalLen = len;
    sawDone = (await page.getByText("完成").count()) > 0;
    if (sawDone && len > 20) break;
  }

  checks.push({
    id: "2-stream-no-retract",
    pass: firstNonZeroAt >= 0 && !sawRetract && finalLen > 0,
    detail: { firstNonZeroAt, maxLen, finalLen, sawRetract, sawDone },
  });

  const domSeparation = await page.locator("article").last().evaluate((article) => {
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
    const answerInsideCard = answerText.length > 0;

    return {
      hasCard: true,
      hasTrace,
      traceCount: uniqueTraces.length,
      traceOutsideCard,
      answerInsideCard,
      answerLen: answerText.length,
      usedTestIds: {
        trace: !!traceByTestId,
        card: !!article.querySelector('[data-testid="turn-answer-card"]'),
      },
    };
  });

  checks.push({
    id: "4-dom-separation",
    pass:
      domSeparation.hasCard &&
      domSeparation.answerInsideCard &&
      domSeparation.traceOutsideCard,
    detail: domSeparation,
  });

  const beforeReload = await readAssistantLen();
  await page.reload();
  await page.waitForSelector("article", { timeout: 10_000 });
  await page.waitForTimeout(1500);
  const afterReload = await readAssistantLen();

  checks.push({
    id: "3-reload-persist",
    pass: afterReload > 0 && afterReload >= beforeReload * 0.8,
    detail: { beforeReload, afterReload },
  });

  const allPass = checks.every((c) => c.pass);
  return { allPass, checks };
};
