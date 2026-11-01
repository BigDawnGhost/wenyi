import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("MinerU saves automatically, shows the saved key masked, and environment wins", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  let stored: string | null = null;
  let status = {
    mode: "manual", storage: null as string | null, available: false,
    environment: "MINERU_API_KEY", system_storage_available: false,
    requires_key: true,
  };
  const writes: unknown[] = [];
  await page.route("**/api/desktop/external-credentials/mineru/secret", route =>
    route.fulfill({ json: { secret: status.mode === "manual" ? stored : null } }));
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      writes.push(body);
      stored = body.clear ? null : body.secret;
      status = { ...status, mode: "manual", storage: body.clear ? null : "session", available: !body.clear };
    }
    return route.fulfill({ json: status });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  const input = card.getByLabel("MinerU API key", { exact: true });
  await expect(card.locator("input")).toHaveCount(1);
  // Only the eye toggle: no Save button.
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.getByRole("button", { name: /Save/ })).toHaveCount(0);
  await expect(card.locator("code")).toHaveCount(0);
  await expect(input).toHaveAttribute("type", "password");
  await card.getByRole("button", { name: "Show MinerU key", exact: true }).click();
  await expect(input).toHaveAttribute("type", "text");
  await card.getByRole("button", { name: "Hide MinerU key", exact: true }).click();
  // Typing pauses, then the key saves once.
  await input.pressSequentially("fake-mineru-test-secret");
  await expect(card.getByRole("status")).toHaveText("MinerU key saved.");
  expect(writes).toEqual([{ secret: "fake-mineru-test-secret" }]);
  await expect(input).toHaveValue("fake-mineru-test-secret");
  await expect(input).toHaveAttribute("type", "password");
  await expect(card).toContainText("Stored for this session only.");
  // Leaving the field with no change writes nothing.
  await input.blur();
  expect(writes).toHaveLength(1);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("fake-mineru");
  await page.reload();
  await expect(input).toHaveValue("fake-mineru-test-secret");
  await expect(input).toHaveAttribute("type", "password");
  await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
  await expect(page.getByRole("textbox", { name: "Advanced YAML configuration" })).not.toHaveValue(/fake-mineru/);
  // Clearing the field removes the key; blur saves without waiting.
  await input.fill("");
  await input.blur();
  await expect(card.getByRole("status")).toHaveText("MinerU key removed.");
  expect(writes[1]).toEqual({ clear: true });
  status = { ...status, mode: "environment", available: true };
  await page.reload();
  await expect(input).toBeDisabled();
  await expect(input).toHaveAttribute("placeholder", "Uses MINERU_API_KEY");
  await expect(input).toHaveValue("");
  expect(writes).toHaveLength(2);
});

test("failed saves are safe and do not retry or claim success", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  let writes = 0;
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    if (route.request().method() === "PUT") {
      writes++;
      return route.fulfill({ status: 503, json: { detail: "fake-sensitive-save-message" } });
    }
    return route.fulfill({ json: {
      mode: "manual", storage: null, available: false, environment: "MINERU_API_KEY",
      system_storage_available: false, requires_key: true,
    } });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  const input = card.getByLabel("MinerU API key", { exact: true });
  await input.fill("fake-failed-mineru");
  await input.blur();
  await expect(card.getByRole("alert")).toHaveText(
    "MinerU credential could not be checked or saved. Please retry.",
  );
  await expect(input).toHaveValue("fake-failed-mineru");
  await expect(card.getByRole("status")).toHaveCount(0);
  await expect(card).not.toContainText("fake-sensitive-save-message");
  await expect(card).not.toContainText("Stored for this session only.");
  // The same failed value is not retried automatically, even after leaving the field again.
  await input.focus();
  await input.blur();
  await page.waitForTimeout(1200);
  expect(writes).toBe(1);
});

test("invalid environment credentials fail closed with actionable local guidance", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  let writes = 0;
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    if (route.request().method() === "PUT") writes++;
    return route.fulfill({ json: {
      mode: "environment", storage: null, available: false, environment: "MINERU_API_KEY",
      system_storage_available: true, requires_key: true,
    } });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  await expect(card.locator("input")).toHaveCount(1);
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.getByLabel("MinerU API key", { exact: true })).toBeDisabled();
  await expect(card.getByRole("button")).toBeDisabled();
  await expect(card.getByRole("alert")).toHaveText(
    "MINERU_API_KEY is not a valid key. Correct it and restart Desktop.",
  );
  await expect(card).not.toContainText("Stored for this session only.");
  expect(writes).toBe(0);
});

test("dirty and pending MinerU writes block signed updates, then release the guard", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
    __TAURI_INTERNALS__: {
      invoke: async (command: string) => command === "desktop_update_status" ? {
        currentVersion: "1.0.0", mode: "automatic", phase: "ready", version: "1.1.0",
        notes: "", downloaded: 100, total: 100,
      } : undefined,
    },
  }));
  await fakeApi(page);
  const status = {
    mode: "manual", storage: "system", available: true, environment: "MINERU_API_KEY",
    system_storage_available: true, requires_key: true,
  };
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route("**/api/desktop/external-credentials/mineru/secret", route =>
    route.fulfill({ json: { secret: "fake-mineru-saved" } }));
  await page.route("**/api/desktop/external-credentials/mineru", async route => {
    if (route.request().method() === "PUT") await gate;
    await route.fulfill({ json: status });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  const install = page.getByRole("button", { name: "Install and restart", exact: true });
  await expect(install).toBeEnabled();
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("fake-mineru-saved");
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-mineru-dirty");
  await expect(install).toBeDisabled();
  await card.getByLabel("MinerU API key", { exact: true }).blur();
  await expect(install).toBeDisabled();
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  // Remounting reloads the saved key; the pending write still blocks installation.
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("fake-mineru-saved");
  await expect(install).toBeDisabled();
  release();
  await expect(install).toBeEnabled();
  // Leaving before the autosave delay discards the edit instead of writing it.
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-mineru-discard");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(install).toBeEnabled();
});

test("local errors are redacted, never retried automatically, and Chinese desktop is usable", async ({ page }) => {
  await page.setViewportSize({ width: 1024, height: 844 });
  await page.clock.install();
  await page.addInitScript(() => {
    localStorage.setItem("wenyi.locale", "zh-CN");
    Object.assign(window, {
      __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
    });
  });
  await fakeApi(page);
  let reads = 0;
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    reads++;
    return route.fulfill({ status: 503, json: { detail: "fake-sensitive-vault-message" } });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  await expect(card).toContainText("MinerU PDF 解析");
  await expect(card.getByRole("alert")).toHaveText("无法检查或保存 MinerU 凭据，请重试。");
  await expect(page.getByText("fake-sensitive-vault-message")).toHaveCount(0);
  await page.evaluate(() => {
    window.dispatchEvent(new Event("offline"));
    window.dispatchEvent(new Event("online"));
    window.dispatchEvent(new Event("focus"));
  });
  await page.clock.runFor(60_000);
  expect(reads).toBe(1);
  await expect(card.locator("input")).toHaveCount(1);
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.getByRole("button", { name: "显示 MinerU 密钥", exact: true })).toBeVisible();
  expect(await card.evaluate(element => element.scrollWidth <= element.clientWidth)).toBe(true);
});

test("saving MinerU neither publishes configuration nor discards the model draft", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  const status = {
    mode: "manual", storage: "system", available: true, environment: "MINERU_API_KEY",
    system_storage_available: true, requires_key: true,
  };
  let mineruWrites = 0;
  let configurationWrites = 0;
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/settings" && request.method() === "PUT")
      configurationWrites++;
  });
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    if (route.request().method() === "PUT") mineruWrites++;
    return route.fulfill({ json: status });
  });
  await page.route("**/api/desktop/credentials", route => route.fulfill({ json: {} }));
  await page.goto("/settings");
  const model = page.getByLabel("Model name", { exact: true });
  await model.fill("independent-model");
  const card = page.getByTestId("mineru-settings");
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-independent-mineru");
  await expect(card.getByRole("status")).toHaveText("MinerU key saved.");
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("fake-independent-mineru");
  await expect(model).toHaveValue("independent-model");
  expect(mineruWrites).toBe(1);
  expect(configurationWrites).toBe(0);
});
