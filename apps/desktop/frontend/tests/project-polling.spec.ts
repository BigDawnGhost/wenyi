import { test } from "@playwright/test";
import { registerTests } from "../../../../packages/ui/tests/e2e/project-polling";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "polling-fixture" },
  }));
});

registerTests();
