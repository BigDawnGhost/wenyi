// Exercise the shared accounting UI through the Desktop entry point as well.
import { test } from "@playwright/test";
import { registerTests } from "../../../../packages/ui/tests/e2e/accounting";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    // Supply native bootstrap while reusing the disposable /api route fixtures.
    Object.assign(window, {
      __WENYI_DESKTOP__: {
        apiBase: `${location.origin}/api`,
        token: "accounting-fixture-token",
      },
    });
  });
});

registerTests();
