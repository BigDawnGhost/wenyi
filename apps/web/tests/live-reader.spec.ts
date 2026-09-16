import { test, expect, type Page } from "@playwright/test";

const pid = "live-book";
const project = {
  id: pid, name: "正在翻译的书", title: "原文", fmt: "epub",
  source_lang: "ja", target_lang: "zh", status: "translating",
  chapter_count: 1, total_word_count: 120, done_chapters: 0, initialized: true,
};
const chapter = {
  index: 0, title: "第一章", title_translated: "第一章", status: "translating",
  word_count: 120, target_word_count: 20, review_issue_count: 0, review_status: "pending",
};
type Segment = { index: number; kind: string; source: string; target: string | null };
type Operation = {
  id: string; chapter_index: number; segment_indices: number[]; status: string;
  applied: number[]; conflicts: number[]; error: string | null; created_at: string;
};

async function setupReader(page: Page) {
  const state = {
    project: { ...project },
    segments: [
      { index: 0, kind: "text", source: "原文第一段", target: "当前译文第一段" },
      { index: 1, kind: "text", source: "原文第二段", target: "" },
      { index: 2, kind: "text", source: "原文第三段", target: null },
      { index: 3, kind: "image", source: "插图", target: "illustration" },
    ] as Segment[],
    operations: [] as Operation[],
    term: { source: "Alice", target: "爱丽丝", type: "person", reading: "", note: "", aliases: [], gender: "" },
    retranslationError: false,
    submitted: [] as number[][],
    chapterReads: 0,
    unexpected: [] as string[],
  };
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname.replace("/api", "");
    const prefix = `/projects/${pid}`;
    if (path === prefix) return route.fulfill({ json: state.project });
    if (path === `${prefix}/chapters`) return route.fulfill({ json: [chapter] });
    if (path === `${prefix}/chapters/0`) {
      state.chapterReads++;
      return route.fulfill({ json: { ...chapter, segments: state.segments, review_issues: [] } });
    }
    if (path === `${prefix}/retranslations`) return route.fulfill({ json: state.operations });
    if (path === `${prefix}/chapters/0/retranslate` && request.method() === "POST") {
      if (state.retranslationError) return route.fulfill({ status: 409, json: { detail: "段落已有重译请求，请稍后重试" } });
      const indices = request.postDataJSON().segment_indices as number[];
      state.submitted.push(indices);
      const operation = {
        id: `re-${state.submitted.length}`, chapter_index: 0, segment_indices: indices,
        status: "queued", applied: [], conflicts: [], error: null, created_at: new Date().toISOString(),
      };
      state.operations.unshift(operation);
      return route.fulfill({ status: 202, json: operation });
    }
    if (path === `${prefix}/glossary/terms`) return route.fulfill({ json: [state.term] });
    if (path === `${prefix}/glossary/terms/Alice` && request.method() === "PUT") {
      state.term = { ...state.term, ...request.postDataJSON() };
      return route.fulfill({ json: state.term });
    }
    if (path === `${prefix}/glossary/conflicts`) return route.fulfill({ json: [] });
    state.unexpected.push(`${request.method()} ${path}`);
    return route.fulfill({ status: 404, json: { detail: `Unexpected request: ${path}` } });
  });
  return state;
}

test("live reading updates finished batches without losing selected paragraphs or scroll", async ({ page }) => {
  const state = await setupReader(page);
  state.segments = Array.from({ length: 70 }, (_, index) => ({
    index, kind: "text", source: `原文第 ${index + 1} 段`,
    target: index < 68 ? `译文第 ${index + 1} 段` : null,
  }));
  await page.goto(`/projects/${pid}/read`);
  await page.getByLabel("选择第 1 段", { exact: true }).check();
  await expect(page.getByText("已译 68 / 70 段", { exact: true })).toBeVisible();
  await expect(page.getByLabel("跟随本章最新译文")).not.toBeChecked();
  await page.locator("main").evaluate((element) => { element.scrollTop = 1000; });
  const scrollTop = await page.locator("main").evaluate((element) => element.scrollTop);
  state.segments[68].target = "刚完成的第 69 段";
  await expect(page.getByText("刚完成的第 69 段")).toBeAttached();
  await expect(page.getByLabel("选择第 1 段", { exact: true })).toBeChecked();
  expect(await page.locator("main").evaluate((element) => element.scrollTop)).toBe(scrollTop);
  expect(state.chapterReads).toBeGreaterThan(1);
  expect(state.unexpected).toEqual([]);
});

test("selects completed text including empty targets and tracks retranslation without pausing", async ({ page }) => {
  const state = await setupReader(page);
  await page.goto(`/projects/${pid}/read`);
  await expect(page.getByLabel("选择第 3 段")).toBeDisabled();
  await expect(page.getByLabel("选择第 4 段")).toBeDisabled();
  await page.getByLabel("选择第 1 段", { exact: true }).check();
  await page.getByLabel("选择第 2 段", { exact: true }).check();
  await page.getByRole("button", { name: "重译所选段落（2）" }).click();
  await expect(page.getByTestId("retranslation-re-1")).toContainText("等待重译");
  await expect(page.getByLabel("选择第 1 段", { exact: true })).not.toBeChecked();
  expect(state.submitted).toEqual([[0, 1]]);
  state.operations[0].status = "running";
  await expect(page.getByTestId("retranslation-re-1")).toContainText("正在重译");
  state.operations[0].status = "done";
  state.operations[0].applied = [0, 1];
  state.segments[0].target = "根据新术语重译的第一段";
  await expect(page.getByTestId("retranslation-re-1")).toContainText("重译完成");
  await expect(page.getByText("根据新术语重译的第一段", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByTestId("retranslation-re-1")).toContainText("已更新段落：1、2");
  expect(state.project.status).toBe("translating");
  expect(state.unexpected).toEqual([]);
});

test("failed submissions keep selection and conflicts preserve current translations", async ({ page }) => {
  const state = await setupReader(page);
  state.retranslationError = true;
  await page.goto(`/projects/${pid}/read`);
  await page.getByLabel("选择第 1 段", { exact: true }).check();
  await page.getByRole("button", { name: "重译所选段落（1）" }).click();
  await expect(page.getByRole("alert")).toContainText("409");
  await expect(page.getByLabel("选择第 1 段", { exact: true })).toBeChecked();
  await expect(page.getByText("段落重译已提交，可继续阅读", { exact: true })).toHaveCount(0);
  state.retranslationError = false;
  await page.getByRole("button", { name: "重译所选段落（1）" }).click();
  await expect(page.getByTestId("retranslation-re-1")).toContainText("等待重译");
  state.operations[0].status = "conflict";
  state.operations[0].conflicts = [0];
  await expect(page.getByTestId("retranslation-re-1")).toContainText("已有更新，保留当前译文");
  await expect(page.getByText("当前译文第一段", { exact: true })).toBeVisible();
  await expect(page.getByLabel("选择第 1 段", { exact: true })).toBeEnabled();
  state.operations[0].status = "error";
  state.operations[0].error = "模型连接失败，请检查供应商设置";
  await expect(page.getByTestId("retranslation-re-1")).toContainText("重译失败");
  await expect(page.getByRole("alert")).toContainText("模型连接失败");
  expect(state.unexpected).toEqual([]);
});

test("edits glossary while translation is running and returns to the same reader selection", async ({ page }) => {
  const state = await setupReader(page);
  await page.goto(`/projects/${pid}/read`);
  await page.getByLabel("选择第 1 段", { exact: true }).check();
  await page.getByRole("button", { name: "编辑术语表", exact: true }).click();
  await expect(page.getByRole("button", { name: "添加术语", exact: true })).toBeEnabled();
  await page.getByRole("button", { name: "编辑术语", exact: true }).click();
  await page.getByLabel("术语译词", { exact: true }).fill("艾丽丝");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => state.term.target).toBe("艾丽丝");
  await expect(page.getByRole("cell", { name: "艾丽丝", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭术语表" }).click();
  await expect(page.getByLabel("选择第 1 段", { exact: true })).toBeChecked();
  await expect(page.getByText("当前译文第一段", { exact: true })).toBeVisible();
  expect(state.project.status).toBe("translating");
  expect(state.unexpected).toEqual([]);
});

test("glossary waits for initialization and subtitle projects keep their dedicated reader", async ({ page }) => {
  const state = await setupReader(page);
  state.project.initialized = false;
  state.project.status = "parsing";
  await page.goto(`/projects/${pid}/glossary`);
  await expect(page.getByRole("button", { name: "添加术语", exact: true })).toBeDisabled();
  await expect(page.getByText("原文初始化完成后即可编辑术语表。", { exact: true })).toBeVisible();
  state.project.fmt = "srt";
  await page.goto(`/projects/${pid}/read`);
  await expect(page.getByRole("link", { name: "打开字幕对照与编辑" })).toHaveAttribute("href", `/projects/${pid}/subtitles`);
  await expect(page.getByRole("button", { name: "重译所选段落" })).toHaveCount(0);
  expect(state.unexpected).toEqual([]);
});
