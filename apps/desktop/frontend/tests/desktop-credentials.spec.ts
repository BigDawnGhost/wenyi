import { expect, test } from "@playwright/test";
import { fakeApi, globalConfiguration } from "./fixtures";

test("Desktop credentials automatically fall back, never echo and explicitly clear", async ({ page }) => {
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: {
        apiBase: "http://127.0.0.1:4174/api",
        token: "fake-desktop-token",
      },
    });
  });
  await fakeApi(page);
  const connection = Object.keys(globalConfiguration.effective.llm.providers)[0];
  let status = {
    mode: "environment",
    storage: null as string | null,
    available: false,
    environment: "WENYI_TEST_FAKE_KEY",
    system_storage_available: false,
    requires_key: true,
  };
  const submissions: Record<string, unknown>[] = [];
  let reads = 0;
  await page.route("**/api/desktop/credentials", (route) => {
    reads++;
    return route.fulfill({ json: { [connection]: status } });
  });
  await page.route(`**/api/desktop/credentials/${connection}`, async (route) => {
    const body = route.request().postDataJSON();
    submissions.push(body);
    status = {
      ...status,
      mode: body.mode,
      storage: body.clear ? null : "session",
      available: !body.clear,
    };
    await route.fulfill({ json: status });
  });
  await page.goto("/settings");
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await expect(page.getByLabel("Credential source", { exact: true })).toHaveCount(0);
  await expect(page.getByLabel("Save key in")).toHaveCount(0);
  const secret = page.getByLabel("API key", { exact: true });
  await expect(secret).toHaveAttribute("type", "password");
  await expect(secret).toHaveValue("");
  await expect(page.getByRole("button", { name: "Save key", exact: true })).toBeDisabled();
  await secret.fill("fake-ui-secret-not-real");
  await page.getByRole("button", { name: "Save key", exact: true }).click();
  await expect(secret).toHaveValue("");
  await expect(page.getByText(/Enter the key again next time/)).toBeVisible();
  expect(reads).toBe(1);
  await expect(page.getByText("Saved source: credential available", { exact: false })).toBeVisible();
  expect(submissions[0]).toEqual({
    mode: "manual",
    secret: "fake-ui-secret-not-real",
  });
  expect(await page.evaluate(() => JSON.stringify(localStorage))).not.toContain("fake-ui-secret");
  await page.reload();
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await expect(page.getByLabel("API key", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "Clear manual key" }).click();
  await expect(page.getByText("Saved source: no credential configured")).toBeVisible();
  expect(submissions[1]).toEqual({ mode: "manual", clear: true });
  await page.getByText("Advanced: environment variable", { exact: true }).click();
  await expect(page.getByLabel("API key environment variable", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Clear manual key and use environment variable" }).click();
  await expect(page.getByRole("button", { name: "Clear manual key", exact: true })).toHaveCount(0);
  expect(submissions[2]).toEqual({ mode: "environment", clear: true });
});

test("credential reads never retry or automatically recheck on focus/reconnect", async ({ page }) => {
  await page.clock.install();
  await page.addInitScript(() => {
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
    });
  });
  await fakeApi(page);
  let reads = 0;
  await page.route("**/api/desktop/credentials", route => {
    reads++;
    return route.fulfill({ status: 503, json: { detail: "Credential store unavailable" } });
  });
  await page.goto("/settings");
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  await expect(page.getByRole("alert")).toContainText("Credential store unavailable");
  await page.clock.runFor(60_000);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("focus"));
    document.dispatchEvent(new Event("visibilitychange"));
  });
  await page.clock.runFor(60_000);
  expect(reads).toBe(1);
  await page.evaluate(() => {
    Object.assign(window, { __WENYI_DESKTOP_BACKGROUND__: true });
    window.dispatchEvent(new Event("wenyi:desktop-visibility"));
    Object.assign(window, { __WENYI_DESKTOP_BACKGROUND__: false });
    window.dispatchEvent(new Event("wenyi:desktop-visibility"));
  });
  await page.clock.runFor(1000);
  expect(reads).toBe(1);
  await page.getByText("Advanced: environment variable", { exact: true }).click();
  await page.getByRole("button", { name: "Check local availability" }).click();
  await expect.poll(() => reads).toBe(2);
});
