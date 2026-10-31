import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";
import type {} from "../src/runtime";

const origin = "http://127.0.0.1:19481";
const term = {
  source: "Alice",
  target: "アリス",
  reading: "ありす",
  type: "person",
  gender: "female",
  aliases: ["Ally", "A."],
  note: "A complete note about this character.\nIncluding a second line.",
};

test.beforeEach(async ({ page }) => {
  await page.addInitScript(origin => {
    window.__WENYI_DESKTOP_PENDING__ = true;
    window.__WENYI_DESKTOP__ = { apiBase: origin, token: "temporary-fixture-token" };
  }, origin);
});

for (const locale of ["en", "zh-CN"] as const) {
  for (const status of [
    "parsing", "queued", "preparing", "translating",
    "reviewing", "autofixing", "postprocessing", "pausing",
  ]) {
    test(`Desktop glossary details remain readable while ${status} in ${locale}`, async ({ page }) => {
      const chinese = locale === "zh-CN";
      await page.addInitScript(
        value => localStorage.setItem("wenyi.locale", value),
        locale,
      );
      await fakeApi(page, {
        [`/projects/${pid}`]: { ...project, status },
        [`/projects/${pid}/glossary/terms`]: [term],
      }, origin);
      const writes: string[] = [];
      page.on("request", request => {
        if (request.url().includes("/glossary/") && request.method() !== "GET")
          writes.push(request.method());
      });
      await page.goto(`/projects/${pid}/glossary`);
      await expect(page.getByRole("button", {
        name: chinese ? "添加术语" : "Add term", exact: true,
      })).toBeDisabled();
      await expect(page.getByRole("button", {
        name: chinese ? "导入" : "Import", exact: true,
      })).toBeDisabled();
      await expect(page.getByRole("button", {
        name: chinese ? "删除术语" : "Delete term", exact: true,
      })).toBeDisabled();

      await page.getByRole("button", {
        name: chinese ? "术语详情" : "Term details", exact: true,
      }).click();
      const details = page.getByRole("dialog", {
        name: chinese ? "术语详情" : "Term details", exact: true,
      });
      await expect(details).toBeVisible();
      const textboxes = details.getByRole("textbox");
      await expect(textboxes).toHaveCount(5);
      for (const [index, value] of [
        term.source, term.target, term.reading, term.aliases.join(", "), term.note,
      ].entries()) {
        const field = textboxes.nth(index);
        await expect(field).toHaveValue(value);
        await expect(field).not.toBeEditable();
        await expect(field).toBeEnabled();
      }
      await expect(details.getByRole("combobox", {
        name: chinese ? "类型" : "Type", exact: true,
      })).toBeDisabled();
      await expect(details.getByRole("combobox", {
        name: chinese ? "性别" : "Gender", exact: true,
      })).toBeDisabled();
      await expect(details.getByRole("button", {
        name: chinese ? "保存" : "Save", exact: true,
      })).toHaveCount(0);
      await page.keyboard.press("Escape");
      await expect(details).toHaveCount(0);
      expect(writes).toEqual([]);
    });
  }
}

test("Desktop open glossary details become editable when the task stops", async ({ page }) => {
  await page.clock.install();
  const saved = { ...project, status: "translating" };
  await fakeApi(page, {
    [`/projects/${pid}`]: saved,
    [`/projects/${pid}/glossary/terms`]: [term],
  }, origin);
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Term details", exact: true }).click();
  const details = page.getByRole("dialog");
  await expect(details.getByRole("textbox").first()).not.toBeEditable();
  saved.status = "paused";
  await page.clock.runFor(4000);
  await expect(details).toHaveAccessibleName("Edit term");
  await expect(details.getByRole("textbox").first()).toBeEditable();
  await expect(details.getByRole("button", { name: "Save", exact: true })).toBeEnabled();
});

test("Desktop open term editors become read-only without losing drafts when a task starts", async ({ page }) => {
  await page.clock.install();
  const saved = { ...project, status: "paused" };
  await fakeApi(page, {
    [`/projects/${pid}`]: saved,
    [`/projects/${pid}/glossary/terms`]: [term],
  }, origin);
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Edit term", exact: true }).click();
  const details = page.getByRole("dialog");
  const notes = details.locator("textarea");
  await notes.fill("Unsaved note");
  await details.getByRole("combobox", { name: "Type", exact: true }).click();
  await expect(page.getByRole("listbox")).toBeVisible();

  await page.evaluate(() => {
    window.__WENYI_DESKTOP_BACKGROUND__ = true;
    window.dispatchEvent(new Event("wenyi:desktop-visibility"));
  });
  saved.status = "queued";
  await page.evaluate(() => {
    window.__WENYI_DESKTOP_BACKGROUND__ = false;
    window.dispatchEvent(new Event("wenyi:desktop-visibility"));
  });
  await page.clock.runFor(1000);
  await expect(details).toHaveAccessibleName("Term details");
  await expect(notes).toHaveValue("Unsaved note");
  await expect(notes).not.toBeEditable();
  await expect(page.getByRole("listbox")).toHaveCount(0);
  await expect(details.getByRole("button", { name: "Save", exact: true })).toHaveCount(0);
});
