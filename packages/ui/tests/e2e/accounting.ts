import { test, expect } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";
import { cacheRate, tokenParts, usageRows } from "../../src/features/progress/accountingData";

export function registerTests() {

function cacheFields(read: number, ordinary: number, write = 0) {
  return {
    cache_hit_tokens: read,
    cache_write_tokens: write,
    uncached_tokens: ordinary,
    cache_unknown_tokens: 0,
    cache_hit_reported_calls: 1,
    cache_write_reported_calls: 1,
    uncached_reported_calls: 1,
  };
}

const modelA = {
  total_tokens: 8000,
  prompt_tokens: 6500,
  completion_tokens: 1500,
  calls: 6,
  cache_miss_tokens: 2500,
  ...cacheFields(4000, 2500),
};
const modelB = {
  total_tokens: 2000,
  prompt_tokens: 1500,
  completion_tokens: 500,
  calls: 2,
  cache_miss_tokens: 700,
  ...cacheFields(800, 700),
};
const stats = {
  usage: {
    schema_version: 3,
    totals: {
      total_tokens: 10000,
      prompt_tokens: 8000,
      completion_tokens: 2000,
      calls: 8,
      cache_miss_tokens: 3200,
      ...cacheFields(4800, 3200),
    },
    by_model: { "model-a": modelA, "model-b": modelB },
    by_provider: { "provider-a": modelA, "provider-b": modelB },
    by_stage: {
      "translation.body": {
        total_tokens: 7000,
        prompt_tokens: 5600,
        completion_tokens: 1400,
        calls: 5,
        cache_miss_tokens: 1800,
        ...cacheFields(3800, 1800),
      },
      "review.verify": {
        total_tokens: 3000,
        prompt_tokens: 2400,
        completion_tokens: 600,
        calls: 3,
        cache_miss_tokens: 1400,
        ...cacheFields(1000, 1400),
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

test("cache partitions preserve input totals and rates require valid cache tokens", () => {
  for (const [cache, expectedRate, unknown] of [
    [cacheFields(60, 20, 20), 0.6, 0],
    [cacheFields(0, 100), 0, 0],
    [{ cache_hit_tokens: 60, cache_miss_tokens: 40 }, 0.6, 40],
    [{ cache_hit_tokens: 0, cache_miss_tokens: 100 }, undefined, 100],
    [{ cache_hit_tokens: 100, cache_miss_tokens: 0 }, 1, 0],
    [{}, undefined, 100],
    [{ cache_hit_tokens: 0, cache_miss_tokens: 0 }, undefined, 100],
    // Legacy misses are ambiguous; the denominator always includes unknown input.
    [{ cache_hit_tokens: 20, cache_miss_tokens: 30 }, 0.2, 80],
    [{ cache_hit_tokens: 120, cache_miss_tokens: 0 }, undefined, 100],
    [{ cache_hit_tokens: 60, cache_miss_tokens: 60 }, undefined, 100],
  ] as const) {
    const slot = {
      prompt_tokens: 100,
      completion_tokens: 25,
      total_tokens: 125,
      ...cache,
    };
    const parts = tokenParts(slot);
    expect(
      (parts.cachedInput ?? 0) +
        (parts.writtenInput ?? 0) +
        (parts.uncachedInput ?? 0) +
        parts.unknownInput,
    ).toBe(100);
    expect(parts.unknownInput).toBe(unknown);
    expect(cacheRate(slot)).toBe(expectedRate);
    expect(parts.total).toBe(125);
  }
});

test("cache rates require the all-input denominator", () => {
  expect(cacheRate({ cache_hit_tokens: 20, cache_miss_tokens: 30 })).toBeUndefined();
});

test("merged model rows preserve known cache subtotals but not incomplete input denominators", () => {
  const rows = usageRows({
    labels: { a: "provider / model", b: "provider / model" },
    by_model: {
      a: { prompt_tokens: 100, total_tokens: 100, ...cacheFields(20, 10, 70) },
      b: { total_tokens: 500 },
    },
  }, "by_model");
  expect(rows).toHaveLength(1);
  expect(rows[0].slot.total_tokens).toBe(600);
  expect(rows[0].slot.cache_write_tokens).toBe(70);
  expect(rows[0].slot.prompt_tokens).toBeUndefined();
  expect(cacheRate(rows[0].slot)).toBeUndefined();
});

test("zero cache writes stay hidden across usage groups without hiding other zero counters", async ({
  page,
}) => {
  const slot = {
    calls: 1,
    prompt_tokens: 100,
    completion_tokens: 0,
    total_tokens: 100,
    ...cacheFields(0, 100),
  };
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        schema_version: 3,
        totals: slot,
        by_stage: { "translation.body": slot },
        by_model: { "model-a": slot },
        by_provider: { "provider-a": slot },
      },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  for (const group of ["By stage", "By model", "By provider"]) {
    await accounting.getByRole("button", { name: group, exact: true }).click();
    const row = accounting.getByRole("list", { name: group, exact: true });
    await expect(row).toContainText("100 tokens");
    await expect(row).toContainText("Cache read input: 0");
    await expect(row).toContainText("Ordinary input: 100");
    await expect(row).toContainText("Output tokens: 0");
    await expect(row).not.toContainText("Cache write input");
  }
  await expect(accounting.locator("dl")).toContainText("100");
  await expect(accounting.locator("dl")).toContainText("0%");
});

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
  await accounting
    .getByRole("button", { name: "By model", exact: true })
    .click();
  await expect(rows).toHaveCount(1);
  await expect(rows).toContainText("10,000 tokens");
  await expect(rows).toContainText("Calls: 8");
  await expect(rows).toContainText("Cache read input: 4,800");
  await expect(rows).toContainText("Ordinary input: 3,200");
  await expect(rows).toContainText("Cache read / all input: 60%");
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
  await expect(accounting).not.toContainText("Reported reads are divided by");
  await expect(
    accounting.getByRole("heading", { name: "Token usage", exact: true }),
  ).toHaveCount(0);
  await expect(accounting.locator("summary")).toHaveCount(0);
  await expect(accounting.getByRole("figure")).toHaveCount(0);
  await expect(accounting.getByRole("group").getByRole("button")).toHaveText([
    "By stage",
    "By model",
    "By provider",
  ]);
  await expect(
    accounting.getByRole("button", { name: "By stage", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await expect(
    accounting.getByRole("list", { name: "By stage", exact: true }),
  ).toContainText("Verify review evidence");
  await accounting
    .getByRole("button", { name: "By model", exact: true })
    .click();
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
  await expect(models.getByRole("listitem").first()).toContainText(
    "Cache read input: 4,000",
  );
  await expect(models.getByRole("listitem").first()).toContainText(
    "Ordinary input: 2,500",
  );
  await expect(models.getByRole("listitem").first()).toContainText(
    "Cache read / all input: 61.5%",
  );
  await accounting
    .getByRole("button", { name: "By provider", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By provider" }),
  ).toContainText("deepseek https://api.deepseek.com/v1");
  await expect(
    accounting
      .getByRole("list", { name: "By provider" })
      .getByRole("listitem")
      .first(),
  ).toContainText("Cache read / all input: 61.5%");
  await accounting
    .getByRole("button", { name: "By stage", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("Verify review evidence");
  await expect(
    accounting
      .getByRole("list", { name: "By stage" })
      .getByRole("listitem")
      .first(),
  ).toContainText("Cache read / all input: 67.9%");
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
    "Prepare translation",
  );
  await accounting.getByRole("button", { name: "Show recent runs" }).click();
  await expect(runs.getByRole("listitem")).toHaveCount(3);
  await expect(
    accounting.getByRole("button", { name: "By stage", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
});

test("Chinese accounting fits compact desktop windows with long provider names and localizes steps", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1024, height: 844 });
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
  await expect(accounting).not.toContainText("已报告缓存读取以全部输入 Token 为分母");
  await expect(accounting.getByRole("group").getByRole("button")).toHaveText([
    "按步骤",
    "按模型",
    "按提供商",
  ]);
  await expect(
    accounting.getByRole("button", { name: "按步骤", exact: true }),
  ).toHaveAttribute("aria-pressed", "true");
  await accounting.locator("dl").scrollIntoViewIfNeeded();
  await expect(accounting.locator("dl")).toContainText("1时30分0秒");
  await page.screenshot({
    path: testInfo.outputPath("accounting-compact-summary.png"),
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
  await expect(page.getByRole("list", { name: "按步骤" })).toContainText(
    "输入 Token（缓存读取）: 3,800",
  );
  await expect(page.getByRole("list", { name: "按步骤" })).toContainText(
    "输入 Token（普通输入）: 1,800",
  );
  await expect(page.getByRole("list", { name: "按步骤" })).not.toContainText(
    "输入 Token（缓存写入）",
  );
  await expect(page.getByRole("list", { name: "按步骤" })).toContainText(
    "缓存读取 / 全部输入: 67.9%",
  );
  await expect(page.getByRole("list", { name: "运行记录" })).toContainText(
    "已中断",
  );
  await expect(page.getByRole("list", { name: "运行记录" })).not.toContainText(
    "completed",
  );
  await page.screenshot({
    path: testInfo.outputPath("accounting-compact.png"),
  });
  await accounting
    .getByRole("region", { name: "运行记录" })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("accounting-compact-runs.png"),
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
  await expect(accounting.getByText("No runs recorded yet.")).toBeVisible();
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
  await accounting
    .getByRole("button", { name: "By stage", exact: true })
    .click();
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("custom.operation");
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("Unclassified tokens: 500");
  await expect(
    accounting.getByRole("list", { name: "By stage" }),
  ).toContainText("Cache read / all input: —");
});

test("unknown cache usage remains distinct from uncached input", async ({
  page,
}) => {
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        totals: {
          prompt_tokens: 1000,
          completion_tokens: 100,
          total_tokens: 1100,
        },
        by_model: {
          "partial-cache": {
            prompt_tokens: 1000,
            completion_tokens: 100,
            total_tokens: 1100,
            cache_hit_tokens: 200,
            cache_miss_tokens: 100,
            calls: 2,
          },
        },
      },
    },
  });
  await page.goto(`/projects/${pid}`);
  const row = page.getByRole("list", { name: "By model", exact: true });
  await page.getByRole("button", { name: "By model", exact: true }).click();
  await expect(row).toContainText("Cache read input: 200");
  await expect(row).toContainText("Cache write input: —");
  await expect(row).toContainText("Ordinary input: —");
  await expect(row).toContainText("Input with unknown cache status: 800");
  // Legacy misses remain unknown; all input stays in the denominator.
  await expect(row).toContainText("Cache read / all input: 20%");
});

test("overreported cache usage shows an unknown rate in totals and model rows", async ({
  page,
}) => {
  const slot = {
    prompt_tokens: 100,
    completion_tokens: 25,
    total_tokens: 125,
    cache_hit_tokens: 60,
    cache_miss_tokens: 60,
    calls: 1,
  };
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: {
        totals: slot,
        by_model: { "overreported-cache": slot },
      },
      timing: { total_seconds: 0, runs: [] },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", {
    name: "Total usage & run time",
  });
  const rateSummary = accounting.locator("dl > div").filter({
    has: page.getByText("Cache read / all input", { exact: true }),
  });
  await expect(rateSummary.locator("dd")).toHaveText("—");
  await accounting.getByRole("button", { name: "By model", exact: true }).click();
  const row = accounting.getByRole("list", { name: "By model", exact: true });
  await expect(row).toContainText("Cache read input: —");
  await expect(row).toContainText("Ordinary input: —");
  await expect(row).toContainText("Input with unknown cache status: 100");
  await expect(row).toContainText("Cache read / all input: —");
});

test("cache writes, ordinary input, unknown coverage and reported TTL stay separate", async ({ page }) => {
  const slot = {
    calls: 2,
    prompt_tokens: 200,
    completion_tokens: 25,
    total_tokens: 225,
    ...cacheFields(20, 10, 70),
    cache_unknown_tokens: 100,
    cache_write_1h_tokens: 70,
    cache_write_1h_reported_calls: 1,
    cache_write_5m_tokens: 0,
    cache_write_5m_reported_calls: 0,
  };
  await fakeApi(page, {
    [`/projects/${pid}/stats`]: {
      usage: { schema_version: 3, totals: slot, by_stage: { "review.scan": slot } },
    },
  });
  await page.goto(`/projects/${pid}`);
  const accounting = page.getByRole("region", { name: "Total usage & run time" });
  const row = accounting.getByRole("list", { name: "By stage", exact: true });
  await expect(row).toContainText("Cache read input: 20");
  await expect(row).toContainText("Cache write input: 70");
  await expect(row).toContainText("Ordinary input: 10");
  await expect(row).toContainText("Input with unknown cache status: 100");
  await expect(row).toContainText("Cache read / all input: 10%");
  await expect(row).toContainText("Cache writes (1h): 70");
  await expect(row).not.toContainText("Cache writes (5m)");
  await expect(accounting).not.toContainText("Unknown classification:");
});
}
