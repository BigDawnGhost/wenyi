import { test, expect } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

const modelA = {
  total_tokens: 8000,
  prompt_tokens: 6500,
  completion_tokens: 1500,
  calls: 6,
  cache_hit_tokens: 4000,
  cache_miss_tokens: 2500,
};
const modelB = {
  total_tokens: 2000,
  prompt_tokens: 1500,
  completion_tokens: 500,
  calls: 2,
  cache_hit_tokens: 800,
  cache_miss_tokens: 700,
};
const stats = {
  usage: {
    schema_version: 2,
    totals: {
      total_tokens: 10000,
      prompt_tokens: 8000,
      completion_tokens: 2000,
      calls: 8,
      cache_hit_tokens: 4800,
      cache_miss_tokens: 3200,
    },
    by_model: { "model-a": modelA, "model-b": modelB },
    by_provider: { "provider-a": modelA, "provider-b": modelB },
    by_stage: {
      "translation.body": {
        total_tokens: 7000,
        prompt_tokens: 5600,
        completion_tokens: 1400,
        calls: 5,
        cache_hit_tokens: 3800,
        cache_miss_tokens: 1800,
      },
      "review.verify": {
        total_tokens: 3000,
        prompt_tokens: 2400,
        completion_tokens: 600,
        calls: 3,
        cache_hit_tokens: 1000,
        cache_miss_tokens: 1400,
      },
    },
    labels: {
      "model-a": "deepseek / deepseek-flash",
      "model-b": "openai / gpt-4.1-mini",
      "provider-a": "deepseek https://api.deepseek.com/v1",
      "provider-b": "openai https://api.openai.com/v1",
    },
  },
  timing: {
    total_seconds: 5400,
    runs: [1800, 1200, 900, 600, 600, 300].map((seconds, index) => ({
      id: `run-${index}`,
      operation: index === 0 ? "prepare" : index === 5 ? "review" : "workflow",
      status: index === 3 ? "interrupted" : "completed",
      started_at: `2026-09-${10 + index}T10:00:00Z`,
      elapsed_seconds: seconds,
    })),
  },
};

test("model usage combines configuration identities with the same provider and model name", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      ...stats,
      usage: {
        ...stats.usage,
        labels: {
          ...stats.usage.labels,
          "model-b": "deepseek / deepseek-flash",
        },
      },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  const rows = accounting
    .getByRole("list", { name: "By model", exact: true })
    .getByRole("listitem");
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("10,000 tokens");
  await expect(rows).toContainText("Calls: 8");
  await expect(rows).toContainText("Input tokens: 8,000");
  await expect(rows).toContainText("Cached tokens: 4,800");
  await accounting
    .getByRole("button", { name: "By provider", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By provider" }).getByRole("listitem"),
  ).toHaveCount(2);
  await expect(accounting.locator("dl")).toContainText("10,000");
});

test("usage charts switch attribution without double counting and retain resumed run history", async ({
  page,
}, testInfo) => {
  await fakeApi(page, { [`/projects/${pid}/stats`]: stats });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  const totals = accounting.locator("dl");
  await expect(totals).toContainText("10,000");
  await expect(totals).toContainText("60%");
  await expect(totals).toContainText("1h 30m 0s");
  await expect(
    accounting.getByRole("region", { name: "Token usage", exact: true }),
  ).toBeVisible();
  await expect(accounting.locator("summary")).toHaveCount(0);
  const composition = accounting.getByRole("figure", {
    name: "Input and output token composition",
  });
  await expect(composition).toContainText("Input tokens 8,000");
  await expect(composition).toContainText("Output tokens 2,000");
  const models = accounting.getByRole("list", {
    name: "By model",
    exact: true,
  });
  await expect(models.getByRole("listitem")).toHaveCount(2);
  await expect(models.getByRole("listitem").first()).toContainText(
    "deepseek / deepseek-flash",
  );
  await expect(models.getByRole("listitem").first()).toContainText(
    "8,000 tokens",
  );
  await accounting
    .getByRole("button", { name: "By provider", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By provider" }),
  ).toContainText("deepseek https://api.deepseek.com/v1");
  await accounting
    .getByRole("button", { name: "By stage", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("Verify review evidence");
  await expect(totals).toContainText("10,000");
  await expect(totals).toContainText("1h 30m 0s");
  const runs = accounting.getByRole("list", {
    name: "Run history",
    exact: true,
  });
  await expect(runs.getByRole("listitem")).toHaveCount(3);
  await expect(runs.getByRole("listitem").first()).toContainText(
    "Whole-book review",
  );
  await expect(runs.getByRole("listitem").first()).toContainText("5m 0s");
  await expect(runs).toContainText("Interrupted");
  await accounting.screenshot({
    path: testInfo.outputPath("accounting-desktop.png"),
  });
  await accounting.getByRole("button", { name: "Show all 6 runs" }).click();
  await expect(runs.getByRole("listitem")).toHaveCount(6);
  await expect(runs.getByRole("listitem").last()).toContainText(
    "Prepare source and glossary",
  );
  await accounting.getByRole("button", { name: "Show recent runs" }).click();
  await expect(runs.getByRole("listitem")).toHaveCount(3);
  await expect(
    accounting.getByRole("button", { name: "By stage", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("Chinese accounting fits narrow screens with long provider names and localizes steps", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      ...stats,
      usage: {
        ...stats.usage,
        labels: {
          ...stats.usage.labels,
          "provider-a": `openai-compatible https://example.invalid/${"long-endpoint/".repeat(8)}`,
        },
      },
    },
  });
  await page.goto(`/projects/${pid}`);
  await expect(
    page.getByRole("heading", { name: "翻译总览", level: 2, exact: true }),
  ).toBeVisible();
  const accounting = page.getByRole("region", { name: "累计用量与运行耗时" });
  await accounting.locator("dl").scrollIntoViewIfNeeded();
  await expect(accounting.locator("dl")).toContainText("1时30分0秒");
  await page.screenshot({
    path: testInfo.outputPath("accounting-mobile-summary.png"),
  });
  await page.getByRole("button", { name: "按提供商", exact: true }).click();
  await expect(
    page.getByRole("list", { name: "按提供商", exact: true }),
  ).toContainText("long-endpoint/");
  expect(
    await page
      .locator("main")
      .evaluate((main) => main.scrollWidth <= main.clientWidth),
  ).toBe(true);
  await page.getByRole("button", { name: "按步骤", exact: true }).click();
  await expect(page.getByRole("list", { name: "按步骤" })).toContainText(
    "审校证据核验",
  );
  await expect(page.getByRole("list", { name: "运行记录" })).toContainText(
    "已中断",
  );
  await expect(page.getByRole("list", { name: "运行记录" })).not.toContainText(
    "completed",
  );
  await page.screenshot({
    path: testInfo.outputPath("accounting-mobile.png"),
  });
  await accounting
    .getByRole("region", { name: "运行记录" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("accounting-mobile-runs.png"),
  });
});

test("empty and partially reported usage shows honest empty states", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        totals: {
          calls: 0,
          total_tokens: 0,
          cache_hit_tokens: 0,
          cache_miss_tokens: 0,
        },
      },
      timing: { total_seconds: 0, runs: [] },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  await expect(accounting.locator("dl")).toContainText("0 s");
  await expect(accounting.locator("dl")).not.toContainText("0%");
  await expect(
    accounting.getByText("No usage breakdown recorded yet."),
  ).toBeVisible();
  await expect(
    accounting.getByText("No completed or stopped runs recorded yet."),
  ).toBeVisible();
  await expect(accounting).not.toContainText("NaN");
  await page.route(`**/api/projects/${pid}/stats`, (route) =>
    route.fulfill({
      json: {
        usage: {
          totals: { total_tokens: 500 },
          by_stage: { "custom.operation": { total_tokens: 500 } },
        },
        timing: {},
      },
    }),
  );
  await page.reload();
  await expect(accounting.getByRole("figure")).toContainText(
    "Unclassified tokens 500",
  );
  await accounting
    .getByRole("button", { name: "By stage", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("custom.operation");
});
