import { test } from "@playwright/test";
import "../../../web/tests/proofreading-performance.spec";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "performance-fixture" },
  }));
});
