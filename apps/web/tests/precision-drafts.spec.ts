import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

const endpoint = `/projects/${pid}/chapters/0/segments/42/precision-drafts`;
const archive = {
  chapter_index: 0,
  segment_index: 42,
  available: true,
  reason: null,
  candidates: [
    { id: "T1", target: "暮色落在河面上，最后一只归鸟掠过柳梢。" },
    { id: "T2", target: "黄昏笼罩河流，归鸟的影子轻轻越过垂柳。" },
    { id: "T3", target: "河上已是薄暮，一只晚归的鸟擦过柳树的枝头。" },
  ],
  synthesized_target: "暮色渐染河面，最后一只归鸟轻掠柳梢。",
  before_polish_candidate: "T1",
};

async function setup(page: Page, busy = false) {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, {
    [`/projects/${pid}`]: { ...project, status: busy ? "translating" : "done" },
    [`/projects/${pid}/review/0`]: {
      index: 0,
      title: "河畔暮色",
      segments: [{
        index: 42,
        source: "Dusk settled over the river; the last homeward bird brushed the willow tips.",
        target: "暮色渐染河面，晚归的鸟轻掠柳梢。（人工校订）",
        kind: "text",
      }],
      review_issues: [],
    },
  });
}

async function open(page: Page) {
  await page.getByRole("button", { name: "段落操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "精翻初稿", exact: true }).click();
}

for (const mobile of [false, true]) {
  test(`three read-only precision drafts in Chinese: ${mobile ? "mobile" : "desktop"}`, async ({ page }, info) => {
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1200 });
    await setup(page, true);
    let requests = 0;
    await page.route(`**/api${endpoint}`, async route => {
      requests++;
      expect(route.request().method()).toBe("GET");
      await route.fulfill({ json: archive });
    });
    await page.goto(`/projects/${pid}/proofreading/0`);
    await expect(page.getByTestId("translation-text")).toBeVisible();
    expect(requests).toBe(0);
    await open(page);
    const dialog = page.getByRole("dialog");
    await expect(dialog.getByText("润色前对照稿", { exact: true })).toBeVisible();
    await expect(dialog.getByText(/最终译文是三稿综合润色，不是选择某一稿/)).toBeVisible();
    for (const candidate of archive.candidates)
      await expect(dialog.getByText(candidate.target, { exact: true })).toBeAttached();
    await expect(dialog.getByText(/当前正式译文与归档综合结果不同/)).toBeAttached();
    await expect(dialog.getByRole("textbox")).toHaveCount(0);
    await expect(dialog.getByRole("button", { name: /发布|保存/ })).toHaveCount(0);
    expect(requests).toBe(1);
    expect(await dialog.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath(`precision-${mobile ? "mobile" : "desktop"}.png`) });
    await dialog.getByText("归档综合润色结果", { exact: true }).scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`precision-${mobile ? "mobile" : "desktop"}-synthesis.png`) });
  });
}

test("lazy draft request preserves unsaved edits and keyboard tabs", async ({ page }) => {
  await setup(page);
  let requests = 0;
  await page.route(`**/api${endpoint}`, async route => {
    requests++;
    await route.fulfill({ json: archive });
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await page.getByRole("button", { name: "段落操作", exact: true }).click();
  await page.getByRole("menuitem", { name: "编辑译文", exact: true }).click();
  await page.getByRole("textbox").fill("未保存的校阅草稿");
  expect(requests).toBe(0);
  await page.getByRole("tab", { name: "译文", exact: true }).focus();
  await page.keyboard.press("End");
  await expect(page.getByRole("tab", { name: "精翻初稿" })).toBeFocused();
  await expect(page.getByText("润色前对照稿")).toBeVisible();
  await page.keyboard.press("ArrowRight");
  await expect(page.getByRole("textbox")).toHaveValue("未保存的校阅草稿");
  await page.getByRole("tab", { name: "精翻初稿" }).click();
  await expect(page.getByText("润色前对照稿")).toBeVisible();
  expect(requests).toBe(1);
});

for (const reason of ["no_archive", "incomplete", "source_mismatch", "ambiguous", "corrupt"]) {
  test(`unavailable archive: ${reason}`, async ({ page }) => {
    await setup(page);
    await page.route(`**/api${endpoint}`, route => route.fulfill({
      json: { ...archive, available: false, reason, candidates: [], synthesized_target: null, before_polish_candidate: null },
    }));
    await page.goto(`/projects/${pid}/proofreading/0`);
    await open(page);
    await expect(page.getByRole("dialog").getByRole("status")).toContainText(
      { no_archive: "没有精翻初稿归档", incomplete: "归档不完整", source_mismatch: "不匹配", ambiguous: "冲突", corrupt: "已损坏" }[reason]!,
    );
    await expect(page.getByText("润色前对照稿")).toHaveCount(0);
  });
}

test("loading and failed request are visible and retryable", async ({ page }) => {
  await setup(page);
  let release!: () => void;
  const gate = new Promise<void>(resolve => { release = resolve; });
  await page.route(`**/api${endpoint}`, async route => {
    await gate;
    await route.fulfill({ status: 503, json: { detail: "Archive unavailable" } });
  });
  await page.goto(`/projects/${pid}/proofreading/0`);
  await open(page);
  await expect(page.getByRole("dialog").getByRole("status")).toBeVisible();
  release();
  await expect(page.getByRole("dialog").getByText(/503: Archive unavailable/)).toBeVisible();
  await page.route(`**/api${endpoint}`, route => route.fulfill({ json: archive }));
  await page.getByRole("button", { name: "重新加载初稿" }).click();
  await expect(page.getByText("润色前对照稿")).toBeVisible();
});

test("an open unavailable draft view refreshes after publication", async ({ page }) => {
  await setup(page, true);
  let published = false;
  await page.route(`**/api${endpoint}`, route => route.fulfill({
    json: published ? archive : {
      ...archive,
      available: false,
      reason: "incomplete",
      candidates: [],
      synthesized_target: null,
      before_polish_candidate: null,
    },
  }));
  await page.goto(`/projects/${pid}/proofreading/0`);
  await open(page);
  await expect(
    page.getByRole("dialog").getByRole("status").filter({ hasText: "归档不完整" }),
  ).toBeVisible();
  published = true;
  await expect(page.getByText("润色前对照稿", { exact: true })).toBeVisible({ timeout: 7000 });
});

test("intentional blank drafts and synthesis display empty-translation labels", async ({ page }) => {
  await setup(page);
  await page.route(`**/api${endpoint}`, route => route.fulfill({
    json: {
      ...archive,
      candidates: archive.candidates.map(candidate => ({ ...candidate, target: "" })),
      synthesized_target: "",
    },
  }));
  await page.goto(`/projects/${pid}/proofreading/0`);
  await open(page);
  await expect(page.getByRole("dialog").getByText("（空译文）", { exact: true })).toHaveCount(4);
});
