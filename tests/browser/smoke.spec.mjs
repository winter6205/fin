// P4-BROWSER-FOUNDATION-01 — the foundation self-check.
//
// It answers one question: does the real application boot on an ephemeral port
// and render its Agent UI? Answer quality, thread journeys, reconnect, and
// attachments belong to P9 and are deliberately absent here.

import { expect, test } from "./fixtures.mjs";

test("P4-BROWSER-FOUNDATION-01: the real app boots and renders", async ({
  appServer,
  page,
  requireBrowserFoundation,
}) => {
  // The built SPA must exist before page content can mean anything.
  expect(requireBrowserFoundation).toBe(true);

  await page.goto(`${appServer.baseUrl}/`);

  // `main.tsx` mounts the SPA into `#root`; the sidebar is app chrome, so its
  // presence proves React rendered rather than the raw index.html shell.
  await expect(page.locator("#root")).toBeVisible();
  await expect(page.getByTestId("sidebar-home-link")).toBeVisible();

  // The same fetch the SPA issues for its boot-scoped status line
  // (`web/src/api.ts: getStatus`), through the page's own origin.
  const status = await page.evaluate(async () => {
    const response = await fetch("/api/status");
    return { ok: response.ok, status: response.status };
  });
  expect(status).toEqual({ ok: true, status: 200 });
});
