import { expect, test } from "@playwright/test";
import { fakeApi, globalConfiguration } from "./fixtures";

test("API keys are typed directly, saved with the configuration and shown masked", async ({ page }) => {
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
  let stored: string | null = null;
  let status = {
    mode: "environment",
    storage: null as string | null,
    available: false,
    environment: "WENYI_TEST_FAKE_KEY",
    system_storage_available: false,
    requires_key: true,
  };
  const order: string[] = [];
  const submissions: Record<string, unknown>[] = [];
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/settings" && request.method() === "PUT")
      order.push("configuration");
  });
  await page.route("**/api/desktop/credentials", (route) =>
    route.fulfill({ json: { [connection]: status } }));
  await page.route(`**/api/desktop/credentials/${connection}/secret`, (route) =>
    route.fulfill({ json: { secret: stored } }));
  await page.route(`**/api/desktop/credentials/${connection}`, async (route) => {
    const body = route.request().postDataJSON();
    order.push("credential");
    submissions.push(body);
    stored = body.clear ? null : body.secret;
    status = {
      ...status,
      mode: body.mode,
      storage: body.clear ? null : "session",
      available: !body.clear,
    };
    await route.fulfill({ json: status });
  });
  await page.goto("/settings");
  // No separate save button and no prerequisite save: the field is ready immediately.
  await expect(page.getByRole("button", { name: "Save key", exact: true })).toHaveCount(0);
  const secret = page.getByLabel("API key", { exact: true });
  const eye = page.getByRole("button", { name: "Show key", exact: true });
  await expect(secret).toBeEnabled();
  await expect(secret).toHaveAttribute("type", "password");
  await expect(eye).toHaveText("");
  await eye.click();
  await expect(secret).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "Hide key", exact: true }).click();
  await expect(secret).toHaveAttribute("type", "password");
  await secret.fill("fake-ui-secret-not-real");
  await page.getByLabel("Model name", { exact: true }).fill("edited-model");
  expect(submissions).toEqual([]);
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect(page.getByText("Global settings saved", { exact: true })).toBeVisible();
  // The saved key stays in the field, masked.
  await expect(secret).toHaveValue("fake-ui-secret-not-real");
  await expect(secret).toHaveAttribute("type", "password");
  await expect(page.getByText(/Enter it again after restarting Desktop/)).toBeVisible();
  // Keys are written only after the connection exists in the saved configuration.
  expect(order).toEqual(["configuration", "credential"]);
  expect(submissions[0]).toEqual({ mode: "manual", secret: "fake-ui-secret-not-real" });
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })))
    .not.toContain("fake-ui-secret");
  await page.reload();
  // After a restart of the page the stored key is loaded back into the masked field.
  await expect(secret).toHaveValue("fake-ui-secret-not-real");
  await expect(secret).toHaveAttribute("type", "password");
  // An unchanged field writes nothing; clearing it removes the stored key on save.
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect(page.getByText("Global settings saved", { exact: true })).toBeVisible();
  expect(submissions).toHaveLength(1);
  await secret.fill("");
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect.poll(() => submissions.length).toBe(2);
  expect(submissions[1]).toEqual({ mode: "manual", clear: true });
  await expect(secret).toHaveAttribute("placeholder", "Enter API key");
  await page.getByText("Environment variable", { exact: true }).click();
  await expect(page.getByLabel("API key environment variable", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Use environment variable instead", exact: true }).click();
  await expect(page.getByRole("button", { name: "Use environment variable instead", exact: true })).toBeDisabled();
  expect(submissions[2]).toEqual({ mode: "environment", clear: true });
});

test("a key entered for a new connection is saved under its renamed ID", async ({ page }) => {
  await page.addInitScript(() => Object.assign(window, {
    __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
  }));
  await fakeApi(page);
  const writes: string[] = [];
  await page.route("**/api/settings", async (route) => {
    if (route.request().method() !== "PUT") return route.fulfill({ json: globalConfiguration });
    const input = route.request().postDataJSON();
    await route.fulfill({ json: {
      ...globalConfiguration, yaml: input.yaml, effective: JSON.parse(input.yaml), revision: 1,
    } });
  });
  await page.route("**/api/desktop/credentials", (route) => route.fulfill({ json: {} }));
  await page.route("**/api/desktop/credentials/*", async (route) => {
    writes.push(decodeURIComponent(new URL(route.request().url()).pathname.split("/").pop()!));
    await route.fulfill({ json: {
      mode: "manual", storage: "system", available: true, environment: null,
      system_storage_available: true, requires_key: true,
    } });
  });
  await page.goto("/settings");
  await page.getByRole("button", { name: "Add API connection", exact: true }).click();
  await page.locator("#credential-secret-provider1").fill("fake-new-connection-key");
  const id = page.getByLabel("Connection ID", { exact: true }).last();
  await id.fill("renamed");
  await id.press("Enter");
  await expect(page.locator("#credential-secret-renamed")).toHaveValue("fake-new-connection-key");
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect.poll(() => writes).toEqual(["renamed"]);
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
  await expect(page.getByRole("alert").filter({ hasText: "Credential store unavailable" })).toBeVisible();
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
  await page.getByText("Environment variable", { exact: true }).click();
  await page.getByRole("button", { name: "Recheck", exact: true }).click();
  await expect.poll(() => reads).toBe(2);
});
