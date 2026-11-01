import { test } from "@playwright/test";

/** Native bootstrap belongs to the host, never the shared UI behavior suites. */
export function registerDesktopSetup(apiOrigin?: string) {
  test.beforeEach(async ({ page }) => {
    await page.addInitScript((origin) => {
      Object.assign(window, {
        __WENYI_DESKTOP__: {
          apiBase: origin ?? `${location.origin}/api`,
          token: "shared-ui-fixture-token",
        },
      });
    }, apiOrigin);
  });
}
