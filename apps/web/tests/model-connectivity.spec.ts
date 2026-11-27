import { expect, test } from "@playwright/test";
import type { ModelPingResult } from "@wenyi/ui/api/types";
import { fakeApi, globalConfiguration } from "../../../packages/ui/sandbox/fixtures";

test("testing edited models uses the current draft without saving or closing", async ({ page }) => {
  const requests: unknown[] = [];
  let writes = 0;
  let saved = globalConfiguration;
  let results: ModelPingResult[] = [
    {
      profile: "deepseek/draft-model",
      connection: "deepseek",
      provider: "deepseek",
      model: "draft-model",
      endpoint: "https://updated.invalid/v1",
      operations: ["translation.body"],
      ok: false,
      category: "authentication_failed",
      message: "Provider authentication failed. Check the API key.",
      status_code: 401,
      latency_ms: 210,
    },
  ];
  await fakeApi(page);
  await page.route("**/api/settings", async route => {
    if (route.request().method() === "PUT") {
      writes++;
      const { yaml } = route.request().postDataJSON();
      saved = { ...saved, yaml, effective: JSON.parse(yaml), revision: saved.revision + 1 };
    }
    await route.fulfill({ json: saved });
  });
  await page.route("**/api/settings/models/ping/draft", async (route) => {
    requests.push(route.request().postDataJSON());
    expect(writes).toBe(0);
    await route.fulfill({ json: results });
  });
  await page.goto("/settings/providers");
  await expect(page.getByRole("button", { name: "Test connection", exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.getByLabel("API base URL", { exact: true }).fill("https://updated.invalid/v1");
  await page.getByLabel("Model name", { exact: true }).fill("draft-model");
  const probe = page.getByRole("button", { name: "Test connection", exact: true });
  await probe.click();
  const status = page.getByRole("status").filter({ hasText: "unreachable" });
  await expect(status).toContainText("1 of 1 models are unreachable.");
  const table = page.getByRole("dialog").getByRole("table");
  await expect(table).toContainText("Authentication failed. Check the API key. (HTTP 401)");
  await expect(table).toContainText("210 ms");
  expect(requests).toEqual([expect.objectContaining({
    connection: "deepseek",
    draft_id: "deepseek",
    provider: expect.objectContaining({ base_url: "https://updated.invalid/v1" }),
    models: { "draft-model": expect.any(Object) },
    api_key: null,
  })]);

  results = [{ ...results[0], ok: true, category: "ok", status_code: null, latency_ms: 95 }];
  await probe.click();
  const success = page.getByRole("status").filter({ hasText: "All 1 models are reachable." });
  await expect(success).toBeVisible();
  const title = page.getByText("Connectivity", { exact: true });
  const description = page.getByText("Send a live request per model using the current edits without saving.", { exact: true });
  await expect(title).toBeVisible();
  await expect(description).toBeVisible();
  await expect.poll(async () => {
    const [heading, text, button, result] = await Promise.all([
      title.boundingBox(), description.boundingBox(), probe.boundingBox(), success.boundingBox(),
    ]);
    return !!heading && !!text && !!button && !!result &&
      heading.y < text.y && text.x + text.width <= button.x &&
      heading.y < button.y + button.height && button.y < text.y + text.height &&
      result.y >= button.y + button.height;
  }).toBe(true);
  await expect(page.getByText("Connected", { exact: true })).toBeVisible();

  expect(requests).toHaveLength(2);
  expect(writes).toBe(0);
  await page.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await expect(page.getByLabel("Model name", { exact: true })).toHaveValue("deepseek-flash");
});

test("a new provider can be tested before it exists in the registry", async ({ page }) => {
  await fakeApi(page);
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Add provider", exact: true }).click();
  const probe = page.getByRole("button", { name: "Test connection", exact: true });
  const hint = page.getByText("Enter a model name for this connection to test it.", { exact: true });
  await expect(probe).toBeDisabled();
  await expect(hint).toBeVisible();
  await page.getByLabel("API base URL", { exact: true }).fill("https://draft.invalid/v1");
  await page.getByLabel("Model name", { exact: true }).fill("new-model");
  await expect(hint).toHaveCount(0);
  await expect(page.getByText("Send a live request per model using the current edits without saving.", { exact: true })).toBeVisible();
  await expect(probe).not.toHaveAttribute("title");
  const request = page.waitForRequest("**/api/settings/models/ping/draft");
  await probe.click();
  expect((await request).postDataJSON()).toMatchObject({
    connection: null, draft_id: "provider1", models: { "new-model": {} },
  });
  await expect(page.getByRole("dialog").getByRole("table")).toContainText("new-model");
  await expect(page.getByRole("dialog")).toBeVisible();
});

test("a slow probe counts elapsed seconds and cannot show results for an older draft", async ({ page }) => {
  await fakeApi(page);
  let release!: () => void;
  const answered = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/settings/models/ping/draft", async (route) => {
    await answered;
    await route.fulfill({ json: [] });
  });
  await page.goto("/settings/providers");
  await page.getByRole("button", { name: "Edit provider deepseek" }).click();
  await page.clock.install({ time: new Date("2025-01-01T00:00:00Z") });
  const probe = page.getByRole("button", { name: "Test connection", exact: true });
  await probe.click();
  await expect(probe).toHaveAttribute("aria-busy", "true");
  await expect(probe).toBeEnabled();
  const elapsed = page.getByText(/elapsed/);
  await expect(elapsed).toHaveText("0 s elapsed");
  await page.clock.runFor(3000);
  await expect(elapsed).toHaveText("3 s elapsed");
  await page.getByLabel("Model name", { exact: true }).fill("newer-model");
  release();
  await expect(elapsed).toHaveCount(0);
  await expect(page.getByText("No models are configured.")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "Test connection", exact: true })).toBeEnabled();
});
