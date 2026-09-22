import { expect, test, type Page } from "@playwright/test";
import { chapter, fakeApi, pid, project } from "./fixtures";

const longTitle =
  "A long title about rivers, mountains and regional traditions ".repeat(8);

function serverState() {
  return {
    status: "done",
    failSave: false,
    conflictOnSave: false,
    rows: [
      {
        ...chapter,
        title: "Opening",
        title_translated: "开篇" as string | null,
      },
      {
        ...chapter,
        index: 7,
        title: longTitle,
        title_translated: null as string | null,
      },
    ],
    writes: [] as unknown[],
  };
}

async function mockContents(page: Page, state = serverState()) {
  await fakeApi(page);
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ json: { ...project, status: state.status } }),
  );
  await page.route(`**/api/projects/${pid}/chapters`, (route) =>
    route.fulfill({ json: state.rows }),
  );
  await page.route(`**/api/projects/${pid}/chapters/*/title`, (route) => {
    expect(route.request().method()).toBe("PUT");
    const index = Number(
      new URL(route.request().url()).pathname.split("/").at(-2),
    );
    const row = state.rows.find((item) => item.index === index)!;
    const body = route.request().postDataJSON();
    state.writes.push(body);
    if (state.failSave)
      return route.fulfill({
        status: 503,
        json: { detail: "Save unavailable" },
      });
    if (state.conflictOnSave) {
      row.title_translated = "Changed before save";
      state.conflictOnSave = false;
    }
    if (body.expected_title_translated !== row.title_translated)
      return route.fulfill({
        status: 409,
        json: {
          detail: "This title changed; reload its latest value before saving",
        },
      });
    row.title_translated = body.title_translated;
    return route.fulfill({
      json: { index, title_translated: row.title_translated },
    });
  });
  return state;
}

for (const chinese of [false, true]) {
  test(`contents titles persist on the server and fit narrow screens in ${chinese ? "Chinese" : "English"}`, async ({
    page,
    browser,
  }, testInfo) => {
    await page.addInitScript(
      (locale) => localStorage.setItem("wenyi.locale", locale),
      chinese ? "zh-CN" : "en",
    );
    const state = await mockContents(page);
    await page.goto(`/projects/${pid}`);
    await page
      .getByRole("link", {
        name: chinese ? "目录与标题" : "Contents & titles",
        exact: true,
      })
      .click();
    const list = page.getByRole("list", {
      name: chinese ? "目录与标题" : "Contents & titles",
    });
    const rows = list.getByRole("listitem");
    await expect(rows).toHaveCount(2);
    await rows
      .first()
      .getByRole("button", { name: chinese ? "编辑标题" : "Edit title" })
      .click();
    const dialog = page.getByRole("dialog");
    const draft = dialog.getByRole("textbox");
    await expect(draft).toHaveValue("开篇");
    await draft.fill("新的开篇");
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(rows.first()).toContainText("开篇");
    await expect(rows.first()).not.toContainText("新的开篇");
    await rows
      .first()
      .getByRole("button", { name: chinese ? "编辑标题" : "Edit title" })
      .click();
    await draft.fill("新的开篇");
    await dialog
      .getByRole("button", { name: chinese ? "保存" : "Save" })
      .click();
    await expect(dialog).toHaveCount(0);
    await page.reload();
    await expect(rows.first()).toContainText("新的开篇");
    expect(state.writes).toEqual([
      { title_translated: "新的开篇", expected_title_translated: "开篇" },
    ]);
    const other = await browser.newContext();
    try {
      const fresh = await other.newPage();
      await mockContents(fresh, state);
      await fresh.goto(new URL(`/projects/${pid}/contents`, page.url()).href);
      await expect(
        fresh.getByRole("list", { name: "Contents & titles" }),
      ).toContainText("新的开篇");
    } finally {
      await other.close();
    }
    const search = page.getByRole("textbox", {
      name: chinese
        ? "搜索原标题或译名"
        : "Search original or translated titles",
    });
    await search.fill("新的开篇");
    await expect(rows).toHaveCount(1);
    await search.clear();
    await expect(rows).toHaveCount(2);
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 960 });
      expect(
        await page
          .getByRole("main")
          .evaluate(
            (element) => element.scrollWidth <= element.clientWidth + 1,
          ),
      ).toBe(true);
      await rows
        .nth(1)
        .getByRole("button", { name: chinese ? "编辑标题" : "Edit title" })
        .click();
      await expect(draft).toHaveValue("");
      await expect(
        dialog.getByRole("button", {
          name: chinese ? "保存" : "Save",
        }),
      ).toBeDisabled();
      await draft.fill("  ");
      await expect(
        dialog.getByRole("button", {
          name: chinese ? "保存" : "Save",
        }),
      ).toBeDisabled();
      expect(
        await dialog.evaluate(
          (element) => element.scrollWidth <= element.clientWidth + 1,
        ),
      ).toBe(true);
      await page.keyboard.press("Escape");
    }
    await page.screenshot({ path: testInfo.outputPath("contents-mobile.png") });
    await rows
      .nth(1)
      .getByRole("button", { name: chinese ? "编辑标题" : "Edit title" })
      .click();
    await draft.fill("第二章");
    await dialog
      .getByRole("button", { name: chinese ? "保存" : "Save", exact: true })
      .click();
    await expect(dialog).toHaveCount(0);
    expect(state.writes.at(-1)).toEqual({
      title_translated: "第二章",
      expected_title_translated: null,
    });
    await rows
      .nth(1)
      .getByRole("link", {
        name: chinese ? "跳转人工校阅" : "Open in proofreading",
      })
      .click();
    await expect(page).toHaveURL(`/projects/${pid}/proofreading/7`);
  });
}

test("a title changed before saving preserves the editor input and requires the latest value", async ({
  page,
}) => {
  const state = await mockContents(page);
  await page.goto(`/projects/${pid}/contents`);
  await page.getByRole("button", { name: "Edit title" }).first().click();
  const dialog = page.getByRole("dialog");
  const draft = dialog.getByRole("textbox");
  await draft.fill("My edit");
  state.conflictOnSave = true;
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("409");
  await expect(draft).toHaveValue("My edit");
  await expect(
    dialog.getByRole("button", { name: "Save", exact: true }),
  ).toBeDisabled();
  await dialog.getByRole("button", { name: "Load latest title" }).click();
  await expect(draft).toHaveValue("Changed before save");
  await draft.fill("Reconciled title");
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.rows[0].title_translated).toBe("Reconciled title");
});

test("polling preserves an unsaved title and disables saving when a task starts", async ({
  page,
}) => {
  const state = await mockContents(page);
  await page.goto(`/projects/${pid}/contents`);
  await page.getByRole("button", { name: "Edit title" }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").fill("Unsaved title");
  state.rows[0].title_translated = "Another editor's title";
  await expect(dialog.getByText(/This title has changed/)).toBeVisible({
    timeout: 8000,
  });
  await expect(dialog.getByRole("textbox")).toHaveValue("Unsaved title");
  state.status = "translating";
  await expect(dialog.getByRole("textbox")).toBeDisabled({ timeout: 8000 });
  await expect(
    dialog.getByRole("button", { name: "Save", exact: true }),
  ).toBeDisabled();
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "Edit title" }).first(),
  ).toBeDisabled();
  expect(state.writes).toEqual([]);
});

test("failed server saves retain input and allow retry without browser draft storage", async ({
  page,
}) => {
  const state = await mockContents(page);
  await page.addInitScript(() => {
    const set = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("wenyi.contentsDrafts:"))
        throw new Error("Obsolete storage");
      set.call(this, key, value);
    };
  });
  await page.goto(`/projects/${pid}/contents`);
  await page.getByRole("button", { name: "Edit title" }).first().click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("textbox").fill("Keep this edit");
  state.failSave = true;
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("Save unavailable");
  await expect(dialog.getByRole("textbox")).toHaveValue("Keep this edit");
  expect(state.rows[0].title_translated).toBe("开篇");
  state.failSave = false;
  await dialog.getByRole("button", { name: "Save", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  expect(state.rows[0].title_translated).toBe("Keep this edit");
});

test("subtitle projects do not expose book contents editing", async ({
  page,
}) => {
  await fakeApi(page, { [`/projects/${pid}`]: { ...project, fmt: "srt" } });
  await page.goto(`/projects/${pid}/contents`);
  await expect(page).toHaveURL(`/projects/${pid}/subtitles`);
  await expect(
    page.getByRole("link", { name: "Contents & titles", exact: true }),
  ).toHaveCount(0);
});
