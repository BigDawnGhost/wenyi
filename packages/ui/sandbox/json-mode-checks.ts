/** Shared browser contracts for Web and Desktop's model configuration flow. */
import { expect, test, type Page } from "@playwright/test";
import { fakeApi, globalConfiguration } from "./fixtures";

export function jsonModeChecks(desktop = false) {
  async function prepare(page: Page) {
    if (desktop) {
      await page.addInitScript(() => {
        Object.assign(window, {
          __WENYI_DESKTOP__: { apiBase: "http://127.0.0.1:4174/api", token: "fixture" },
        });
      });
    }
    const saved = structuredClone(globalConfiguration);
    saved.effective.llm.providers.default = {
      kind: "openai-compatible",
    };
    Object.assign(saved.effective.llm.providers.default, { base_url: "http://localhost:1234/v1" });
    Object.assign(saved.effective.llm.models.default_model, {
      options: { thinking: false, request_overrides: { temperature: 0.5 } },
    });
    saved.yaml = JSON.stringify(saved.effective);
    await fakeApi(page, { "/settings": saved });
    return saved;
  }

  test("detected JSON modes are applied to the draft and persisted by saving", async ({ page }) => {
    let saved = await prepare(page);
    let writes = 0;
    await page.route("**/api/settings", async (route) => {
      if (route.request().method() === "PUT") {
        const input = route.request().postDataJSON();
        expect(input.revision).toBe(saved.revision);
        writes++;
        saved = { ...saved, ...input, effective: JSON.parse(input.yaml), revision: saved.revision + 1 };
      }
      await route.fulfill({ json: saved });
    });
    await page.route("**/api/settings/models/ping", async (route) => {
      expect(route.request().postDataJSON()).toEqual({ detect_json: true });
      await route.fulfill({ json: [{
        profile: "default_model", connection: "default", provider: "openai-compatible",
        model: "deepseek-flash", operations: ["translation.body"], ok: true,
        category: "ok", message: "The provider replied.", latency_ms: 25,
        json_mode: "json_schema", json_category: "ok",
      }] });
    });
    await page.goto("/settings/providers");
    await page.getByRole("button", { name: "Test connection", exact: true }).click();
    await expect(page.getByRole("cell", { name: "json_schema", exact: true })).toBeVisible();
    expect(writes).toBe(0);
    await page.getByRole("button", { name: "Apply detected JSON modes" }).click();
    await expect(page.getByText("JSON modes applied to the draft. Save the configuration to use them.")).toBeVisible();
    expect(writes).toBe(0);
    await expect(page.getByLabel("JSON mode", { exact: true })).toHaveText("json_schema");
    await page.getByRole("button", { name: "Save configuration", exact: true }).click();
    await expect(page.getByText("Global settings saved", { exact: true })).toBeVisible();
    await expect(page.getByText("JSON modes applied to the draft. Save the configuration to use them.")).toHaveCount(0);
    expect(writes).toBe(1);
    expect(saved.effective.llm.models.default_model).toMatchObject({
      options: { thinking: false, json_output_mode: "json_schema", request_overrides: { temperature: 0.5 } },
    });
    await page.reload();
    await expect(page.getByLabel("JSON mode", { exact: true })).toHaveText("json_schema");
  });

  test("an edited configuration cannot use delayed JSON detection results", async ({ page }) => {
    await prepare(page);
    let release!: () => void;
    const answered = new Promise<void>((resolve) => { release = resolve; });
    await page.route("**/api/settings/models/ping", async (route) => {
      await answered;
      await route.fulfill({ json: [{
        profile: "default_model", connection: "default", provider: "openai-compatible",
        model: "deepseek-flash", operations: [], ok: true, category: "ok",
        message: "The provider replied.", json_mode: "json_schema", json_category: "ok",
      }] });
    });
    await page.goto("/settings/providers");
    await page.getByRole("button", { name: "Test connection", exact: true }).click();
    await page.getByLabel("API base URL", { exact: true }).fill("https://changed.invalid/v1");
    release();
    await expect(page.getByRole("button", { name: "Test connection", exact: true })).toBeDisabled();
    await expect(page.getByRole("button", { name: "Apply detected JSON modes" })).toHaveCount(0);
    await expect(page.getByLabel("JSON mode", { exact: true })).toHaveText("json_object");
  });

  test("JSON failures are distinct from connectivity and cannot be applied", async ({ page }) => {
    await prepare(page);
    await page.route("**/api/settings/models/ping", async (route) => {
      await route.fulfill({ json: [{
        profile: "default_model", connection: "default", provider: "openai-compatible",
        model: "deepseek-flash", operations: [], ok: true, category: "ok",
        message: "The provider replied.", json_category: "invalid_json_response",
      }] });
    });
    await page.goto("/settings/providers");
    await page.getByRole("button", { name: "Test connection", exact: true }).click();
    await expect(page.getByText("Connected", { exact: true })).toBeVisible();
    await expect(page.getByText("The model did not return the complete JSON test object.")).toBeVisible();
    await expect(page.getByRole("button", { name: "Apply detected JSON modes" })).toHaveCount(0);
  });
}
