import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { fakeApi, pid, termPageFixture } from "./fixtures";
import { chooseOption } from "./select-helper";

const origin = "http://127.0.0.1:19481";
const term = (source: string, type = "person") => ({
  source, target: `Translation ${source}`, type, reading: "", note: "", gender: "", aliases: [],
});

async function setup(page: Page, info: TestInfo, terms: ReturnType<typeof term>[]) {
  if (info.project.name === "desktop") {
    await page.addInitScript((apiBase) => {
      Object.assign(window, {
        __WENYI_DESKTOP__: { apiBase, token: "pagination-fixture-token" },
      });
    }, origin);
  }
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: terms },
    info.project.name === "desktop" ? origin : undefined);
}

for (const locale of ["en", "zh-CN"] as const) {
  test(`glossary keeps one top pager aligned with filters in ${locale}`, async ({ page }, info) => {
    await page.addInitScript((value) => localStorage.setItem("wenyi.locale", value), locale);
    await setup(page, info, Array.from({ length: 284 }, (_, i) => term(`Name ${i}`)));
    await page.goto(`/projects/${pid}/glossary`);
    await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(100);
    const pager = page.getByRole("navigation", {
      name: locale === "en" ? "Glossary pages" : "术语分页",
      exact: true,
    });
    await expect(pager).toHaveCount(1);
    await expect(page.getByRole("button", {
      name: locale === "en" ? "Previous page" : "上一页", exact: true,
    })).toHaveCount(1);
    await expect(page.getByRole("button", {
      name: locale === "en" ? "Next page" : "下一页", exact: true,
    })).toHaveCount(1);
    for (const width of [1440, 1200, 900, 390]) {
      await page.setViewportSize({ width, height: 960 });
      const search = await page.getByRole("main").getByRole("textbox").boundingBox();
      const filter = await page.getByRole("combobox").boundingBox();
      const pagination = await pager.boundingBox();
      const table = await page.getByRole("table").boundingBox();
      expect(pagination!.y + pagination!.height).toBeLessThanOrEqual(table!.y);
      if (width >= 1200) {
        expect(Math.abs(search!.y + search!.height / 2 - filter!.y - filter!.height / 2))
          .toBeLessThanOrEqual(1);
        expect(Math.abs(search!.y + search!.height / 2 - pagination!.y - pagination!.height / 2))
          .toBeLessThanOrEqual(1);
      }
      expect(await page.getByRole("main").evaluate(
        (element) => element.scrollWidth <= element.clientWidth + 1,
      )).toBe(true);
    }
  });
}

test("large glossary renders only a page and resets selection and filters", async ({ page }, info) => {
  const terms = Array.from({ length: 10_000 }, (_, i) =>
    term(`Term ${String(i).padStart(5, "0")}`, i % 2 ? "term" : "person"),
  ).reverse();
  await setup(page, info, terms);
  const queries: string[] = [];
  const fullReads: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes("/glossary/terms/page"))
      queries.push(new URL(request.url()).searchParams.get("q") || "");
    if (request.method() === "GET" && new URL(request.url()).pathname.endsWith("/glossary/terms"))
      fullReads.push(request.url());
  });
  await page.goto(`/projects/${pid}/glossary`);
  const rows = page.getByRole("table").locator("tbody tr");
  await expect(rows).toHaveCount(100);
  await expect(rows.first()).toContainText("Term 00000");
  await expect(rows.last()).toContainText("Term 00099");
  await page.getByRole("checkbox", { name: "Select all terms on this page" }).check();
  await expect(page.getByRole("button", { name: "Delete selected", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Next page", exact: true }).first().click();
  await expect(rows.first()).toContainText("Term 00100");
  await expect(page.getByRole("button", { name: "Delete selected", exact: true })).toHaveCount(0);
  await expect(page.getByRole("checkbox", { checked: true })).toHaveCount(0);
  await chooseOption(page.getByRole("combobox", { name: "Type", exact: true }), "Person");
  await expect(rows.first()).toContainText("Term 00000");
  await expect(rows.nth(1)).toContainText("Term 00002");
  await page.getByRole("button", { name: "Next page", exact: true }).first().click();
  await expect(rows.first()).toContainText("Term 00200");
  await chooseOption(page.getByRole("combobox", { name: "Type", exact: true }), "All types");
  await expect(rows.first()).toContainText("Term 00000");
  await chooseOption(page.getByRole("combobox", { name: "Type", exact: true }), "Person");
  await expect(rows.first()).toContainText("Term 00000");
  const search = page.getByRole("main").getByRole("textbox");
  await search.pressSequentially("Term 09998", { delay: 10 });
  await expect(rows).toHaveCount(1);
  await expect(rows.first()).toContainText("Term 09998");
  await expect(page.getByRole("button", { name: "Previous page", exact: true }).first()).toBeDisabled();
  expect(queries.filter(Boolean)).toEqual(["Term 09998"]);
  await search.fill("no matching source");
  await expect(page.getByText("No matching terms", { exact: true })).toBeVisible();
  // No legacy full-glossary read is needed to display/search pages.
  expect(fullReads).toEqual([]);
});

test("deleting the last term of a page returns to the remaining last page", async ({ page }, info) => {
  let terms = Array.from({ length: 101 }, (_, i) => term(`Term ${String(i).padStart(3, "0")}`));
  await setup(page, info, terms);
  await page.route(`**/projects/${pid}/glossary/terms/page?**`, (route) =>
    route.fulfill({ json: termPageFixture(terms, route.request().url()) }),
  );
  await page.route(`**/projects/${pid}/glossary/terms/Term%20100`, (route) => {
    terms = terms.filter((entry) => entry.source !== "Term 100");
    return route.fulfill({ json: { message: "deleted" } });
  });
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Next page", exact: true }).first().click();
  await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(1);
  await page.getByRole("button", { name: "Delete term", exact: true }).click();
  await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(100);
  await expect(page.getByRole("button", { name: "Next page", exact: true }).first()).toBeDisabled();
  await expect(page.getByRole("status").filter({ hasText: "Page 1 / 1 · Terms: 100" }).first()).toBeVisible();
});

test("import detects existing terms outside the displayed page and type", async ({ page }, info) => {
  const hidden = term("ZZZ hidden", "term");
  await setup(page, info, [...Array.from({ length: 100 }, (_, i) => term(`Name ${i}`)), hidden]);
  await page.goto(`/projects/${pid}/glossary?type=person`);
  await expect(page.getByRole("table").locator("tbody tr")).toHaveCount(100);
  await expect(page.getByRole("cell", { name: hidden.source, exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "Import", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Import terms", exact: true });
  await dialog.locator('input[type="file"]').setInputFiles({
    name: "terms.json", mimeType: "application/json",
    buffer: Buffer.from(JSON.stringify([{ source: hidden.source, target: "New translation" }])),
  });
  await expect(dialog).toContainText(hidden.target);
  await expect(dialog).toContainText("New translation");
  await expect(dialog.getByRole("button", { name: "Confirm import", exact: true })).toBeVisible();
});

test("a superseded search is cancelled and cannot replace current results", async ({ page }, info) => {
  await setup(page, info, []);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  await page.route(`**/projects/${pid}/glossary/terms/page?**`, async (route) => {
    const query = new URL(route.request().url()).searchParams.get("q");
    if (query === "old") await gate;
    return route.fulfill({ json: { items: query ? [term(query)] : [], total: query ? 1 : 0 } });
  });
  await page.goto(`/projects/${pid}/glossary`);
  const search = page.getByRole("main").getByRole("textbox");
  const oldRequest = page.waitForRequest((request) =>
    new URL(request.url()).searchParams.get("q") === "old",
  );
  await search.fill("old");
  await oldRequest;
  const cancelled = page.waitForEvent("requestfailed", {
    predicate: (request) => new URL(request.url()).searchParams.get("q") === "old",
  });
  try {
    await search.fill("new");
    await expect(page.getByRole("cell", { name: "new", exact: true })).toBeVisible();
    await cancelled;
  } finally {
    release();
  }
  await expect(page.getByRole("cell", { name: "old", exact: true })).toHaveCount(0);
});
