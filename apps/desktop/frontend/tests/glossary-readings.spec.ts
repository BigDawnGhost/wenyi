import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "../../../../packages/ui/sandbox/fixtures";
import { registerDesktopSetup } from "./shared-setup";

const origin = "http://127.0.0.1:19481";
registerDesktopSetup(origin);

const term = {
  source: "Fixture name",
  target: "Original translation",
  reading: "stored-reading",
  type: "person",
  note: "Original note",
  gender: "",
  aliases: [],
};

test("Desktop sends hidden readings to the local API and shows them after language detection", async ({ page }) => {
  const book = { ...project, source_lang: "auto", target_lang: "en" };
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await fakeApi(page, {
    [`/projects/${pid}`]: book,
    [`/projects/${pid}/glossary/terms`]: [term],
  }, origin);
  await page.goto(`/projects/${pid}/glossary`);

  const edit = page.getByRole("button", { name: "编辑术语", exact: true });
  const dialog = page.getByRole("dialog");
  const reading = dialog.getByRole("textbox", { name: "读音", exact: true });
  await edit.click();
  await expect(dialog).toBeVisible();
  await expect(reading).toHaveCount(0);
  await dialog.locator("textarea").fill("Updated note");
  const update = page.waitForRequest(request =>
    request.method() === "PUT" &&
    request.url() === `${origin}/projects/${pid}/glossary/terms/${encodeURIComponent(term.source)}`,
  );
  await dialog.getByRole("button", { name: "保存", exact: true }).click();
  expect((await update).postDataJSON()).toMatchObject({ ...term, note: "Updated note" });
  await expect(dialog).not.toBeVisible();

  book.source_lang = "ja";
  await page.reload();
  await page.getByRole("button", { name: "添加术语", exact: true }).click();
  await expect(reading).toBeVisible();
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await edit.click();
  await expect(reading).toHaveValue(term.reading);
});
