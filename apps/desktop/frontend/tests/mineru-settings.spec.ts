import { expect, test } from "@playwright/test";
import { fakeApi } from "./fixtures";

test("MinerU saves independently, never echoes, and environment wins over fallback", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  let status = {
    mode: "manual", storage: null as string | null, available: false,
    environment: "MINERU_API_KEY", system_storage_available: false,
    requires_key: true,
  };
  const writes: unknown[] = [];
  await page.route("**/api/desktop/external-credentials/mineru", route => {
    if (route.request().method() === "PUT") {
      const body = route.request().postDataJSON();
      writes.push(body);
      status = { ...status, mode: "manual", storage: "session", available: true };
    }
    return route.fulfill({ json: status });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  const input = card.getByLabel("MinerU API key", { exact: true });
  await expect(card.locator("input")).toHaveCount(1);
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.locator("code")).toHaveCount(0);
  await expect(input).toHaveAttribute("type", "password");
  await input.fill("fake-mineru-test-secret");
  await card.getByRole("button", { name: "Save MinerU key", exact: true }).click();
  await expect(input).toHaveValue("");
  await expect(card.getByRole("status")).toHaveText("MinerU key saved.");
  await expect(card).toContainText("Stored for this session only.");
  await expect(card.getByRole("button")).toHaveCount(1);
  expect(writes).toEqual([{ secret: "fake-mineru-test-secret" }]);
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain("fake-mineru");
  await page.reload();
  await expect(card).toContainText("Stored for this session only.");
  await expect(input).toHaveAttribute("placeholder", "Key saved · enter to replace");
  await expect(card.getByRole("button")).toHaveCount(1);
  await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
  await expect(page.getByRole("textbox", { name: "Advanced YAML configuration" })).not.toHaveValue(/fake-mineru/);
  status = { ...status, mode: "environment", available: true };
  await page.reload();
  await expect(input).toBeDisabled();
  await expect(input).toHaveAttribute("placeholder", "Uses MINERU_API_KEY");
  await expect(card.getByRole("button")).toHaveCount(1);
  await expect(card.getByRole("button")).toBeDisabled();
  await expect(input).toHaveValue("");
  expect(writes).toEqual([{ secret: "fake-mineru-test-secret" }]);
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
  await card.getByRole("button", { name: "Save MinerU key", exact: true }).click();
  await expect(card.getByRole("alert")).toHaveText(
    "MinerU credential could not be checked or saved. Please retry.",
  );
  await expect(input).toHaveValue("fake-failed-mineru");
  await expect(card.getByRole("status")).toHaveCount(0);
  await expect(card).not.toContainText("fake-sensitive-save-message");
  await expect(card).not.toContainText("Stored for this session only.");
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
  await page.route("**/api/desktop/external-credentials/mineru", async route => {
    if (route.request().method() === "PUT") await gate;
    await route.fulfill({ json: status });
  });
  await page.goto("/settings");
  const card = page.getByTestId("mineru-settings");
  const install = page.getByRole("button", { name: "Install and restart", exact: true });
  await expect(install).toBeEnabled();
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-mineru-dirty");
  await expect(install).toBeDisabled();
  await card.getByRole("button", { name: "Save MinerU key", exact: true }).click();
  await expect(install).toBeDisabled();
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("");
  await expect(install).toBeDisabled();
  release();
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("");
  await expect(install).toBeEnabled();
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-mineru-discard");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await expect(install).toBeEnabled();
});

test("local errors are redacted, never retried automatically, and Chinese mobile is usable", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
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
  await expect(card).toContainText("MinerU PDF 解析凭据");
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
  await expect(card.getByRole("button")).toHaveText("保存 MinerU 密钥");
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
  await page.locator("summary").filter({ hasText: "API providers & models" }).click();
  const model = page.getByLabel("Model name", { exact: true });
  await model.fill("independent-model");
  const card = page.getByTestId("mineru-settings");
  await card.getByLabel("MinerU API key", { exact: true }).fill("fake-independent-mineru");
  await card.getByRole("button", { name: "Save MinerU key", exact: true }).click();
  await expect(card.getByLabel("MinerU API key", { exact: true })).toHaveValue("");
  await expect(model).toHaveValue("independent-model");
  expect(mineruWrites).toBe(1);
  expect(configurationWrites).toBe(0);
});
