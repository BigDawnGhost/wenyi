import { test } from "@playwright/test";
import { registerShellTests } from "../../../../packages/ui/tests/e2e/shell";

test.beforeEach(async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: {
        apiBase: "http://127.0.0.1:19485",
        token: "unified-shell-fixture-token",
      },
    });
  });
});

registerShellTests({ apiOrigin: "http://127.0.0.1:19485" });
