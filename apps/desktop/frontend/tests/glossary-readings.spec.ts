import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";
import type {} from "../src/runtime";

const origin = "http://127.0.0.1:19481";
const term = {
  source: "Fixture name",
  target: "Original translation",
  reading: "stored-reading",
  type: "person",
  note: "Original note",
  gender: "",
  aliases: [],
};

async function desktopGlossary(page: Page, locale: string, sourceLang: string) {
  await page.addInitScript(({ origin, locale }) => {
    localStorage.setItem("wenyi.locale", locale);
    window.__WENYI_DESKTOP_PENDING__ = true;
    window.__WENYI_DESKTOP__ = { apiBase: origin, token: "temporary-fixture-token" };
  }, { origin, locale });
  await fakeApi(page, {
    [`/projects/${pid}`]: {
      ...project,
      source_lang: sourceLang,
      target_lang: sourceLang === "ja" ? "en" : "ja",
    },
    [`/projects/${pid}/glossary/terms`]: [term],
  }, origin);
  await page.goto(`/projects/${pid}/glossary`);
  await expect(page.getByRole("cell", { name: term.source, exact: true })).toBeVisible();
}

for (const locale of ["en", "zh-CN"] as const) {
  const chinese = locale === "zh-CN";
  const reading = chinese ? "读音" : "Reading";
  const addTerm = chinese ? "添加术语" : "Add term";
  const editTerm = chinese ? "编辑术语" : "Edit term";
  const cancel = chinese ? "取消" : "Cancel";
  const save = chinese ? "保存" : "Save";

  for (const sourceLang of ["ja", "en", "auto"] as const) {
    test(`Desktop glossary readings follow ${sourceLang} source in ${locale}`, async ({ page }) => {
      await desktopGlossary(page, locale, sourceLang);
      await expect(page.getByRole("columnheader", { name: /^(Reading|读音)$/ })).toHaveCount(0);
      if (sourceLang !== "ja") {
        await expect(page.locator("input[placeholder]").first())
          .not.toHaveAttribute("placeholder", /readings?|读音/i);
      }

      await page.getByRole("button", { name: addTerm, exact: true }).click();
      const addDialog = page.getByRole("dialog", { name: addTerm, exact: true });
      await expect(addDialog).toBeVisible();
      const addReading = addDialog.getByRole("textbox", { name: reading, exact: true });
      if (sourceLang === "ja") {
        await expect(addReading).toBeVisible();
        await addReading.fill("new-reading");
      } else {
        await expect(addReading).toHaveCount(0);
        await expect(addDialog.getByText(reading, { exact: true })).toHaveCount(0);
      }
      await addDialog.getByRole("button", { name: cancel, exact: true }).click();

      await page.getByRole("button", { name: editTerm, exact: true }).click();
      const editDialog = page.getByRole("dialog", { name: editTerm, exact: true });
      await expect(editDialog).toBeVisible();
      const editReading = editDialog.getByRole("textbox", { name: reading, exact: true });
      if (sourceLang === "ja") {
        await expect(editReading).toHaveValue(term.reading);
      } else {
        await expect(editReading).toHaveCount(0);
        await expect(editDialog.getByText(reading, { exact: true })).toHaveCount(0);
      }

      // Notes are the only multiline textbox; changing them must not erase hidden readings.
      await editDialog.locator("textarea").fill("Updated note");
      const update = page.waitForRequest(request =>
        request.url().startsWith(`${origin}/projects/${pid}/glossary/terms/`) &&
        request.method() === "PUT",
      );
      await editDialog.getByRole("button", { name: save, exact: true }).click();
      expect((await update).postDataJSON()).toMatchObject({
        source: term.source,
        target: term.target,
        reading: term.reading,
        note: "Updated note",
      });
      await expect(editDialog).not.toBeVisible();
    });
  }

  test(`Desktop detected Japanese readings appear after refresh in ${locale}`, async ({ page }) => {
    await desktopGlossary(page, locale, "auto");
    await page.getByRole("button", { name: addTerm, exact: true }).click();
    const dialog = page.getByRole("dialog", { name: addTerm, exact: true });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByText(reading, { exact: true })).toHaveCount(0);
    await dialog.getByRole("button", { name: cancel, exact: true }).click();

    // Simulate source-language detection without starting a real Desktop task.
    await page.route(`${origin}/projects/${pid}`, route => route.fulfill({
      json: { ...project, source_lang: "ja", target_lang: "ja" },
    }));
    await page.reload();
    await expect(page.getByRole("cell", { name: term.source, exact: true })).toBeVisible();
    await page.getByRole("button", { name: addTerm, exact: true }).click();
    await expect(dialog.getByRole("textbox", { name: reading, exact: true })).toBeVisible();
    await dialog.getByRole("button", { name: cancel, exact: true }).click();
    await page.getByRole("button", { name: editTerm, exact: true }).click();
    await expect(page.getByRole("dialog", { name: editTerm, exact: true })
      .getByRole("textbox", { name: reading, exact: true })).toHaveValue(term.reading);
    await expect(page.getByRole("columnheader", { name: /^(Reading|读音)$/ })).toHaveCount(0);
  });
}
