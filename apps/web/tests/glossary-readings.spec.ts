import { expect, test } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";

const term = {
  source: "Alice",
  target: "アリス",
  reading: "あいす",
  type: "person",
  gender: "female",
  aliases: [],
  note: "",
};

for (const locale of ["en", "zh-CN"] as const) {
  for (const source of ["ja", "en", "vi", "auto"]) {
    test(`glossary readings stay in Japanese-source details: ${source}, ${locale}`, async ({
      page,
    }) => {
      const japanese = source === "ja";
      const chinese = locale === "zh-CN";
      const readingLabel = chinese ? "读音" : "Reading";
      await page.addInitScript(
        (value) => localStorage.setItem("wenyi.locale", value),
        locale,
      );
      await fakeApi(page, {
        [`/projects/${pid}`]: {
          ...project,
          source_lang: source,
          target_lang: japanese ? "en" : "ja",
        },
        [`/projects/${pid}/glossary/terms`]: [term],
      });
      await page.route(`**/projects/${pid}/glossary/terms/${term.source}`, (route) =>
        route.fulfill({ json: { ...term, ...route.request().postDataJSON() } }),
      );
      await page.goto(`/projects/${pid}/glossary`);
      const table = page.getByRole("table");
      await expect(table.getByRole("columnheader", { name: readingLabel })).toHaveCount(0);
      await expect(table.getByRole("columnheader")).toHaveCount(5);
      await expect(table).not.toContainText(term.reading);
      await expect(page.getByRole("main").getByRole("textbox")).toHaveAttribute(
        "placeholder",
        japanese ? /readings|读音/ : /aliases,? or notes|别名 \/ 备注/,
      );

      await page.getByRole("button", {
        name: chinese ? "编辑术语" : "Edit term",
        exact: true,
      }).click();
      const details = page.getByRole("dialog");
      const reading = details.getByRole("textbox", { name: readingLabel, exact: true });
      if (japanese) {
        await expect(reading).toHaveValue(term.reading);
        await reading.fill("あいりす");
      } else {
        await expect(details.getByText(readingLabel, { exact: true })).toHaveCount(0);
        await expect(reading).toHaveCount(0);
      }
      await details.getByRole("textbox").nth(1).fill("Updated translation");
      const saved = page.waitForRequest((request) =>
        request.method() === "PUT" && request.url().endsWith(`/glossary/terms/${term.source}`),
      );
      await details.getByRole("button", {
        name: chinese ? "保存" : "Save",
        exact: true,
      }).click();
      expect((await saved).postDataJSON()).toMatchObject({
        target: "Updated translation",
        reading: japanese ? "あいりす" : term.reading,
      });
      await expect(details).toHaveCount(0);

      await page.getByRole("button", {
        name: chinese ? "添加术语" : "Add term",
        exact: true,
      }).click();
      const add = page.getByRole("dialog");
      const newReading = add.getByRole("textbox", { name: readingLabel, exact: true });
      if (japanese) {
        await expect(newReading).toBeVisible();
        await newReading.fill("あたらしい");
      } else {
        await expect(add.getByText(readingLabel, { exact: true })).toHaveCount(0);
        await expect(newReading).toHaveCount(0);
      }
      await add.getByRole("textbox").nth(0).fill("New term");
      await add.getByRole("textbox").nth(1).fill("New translation");
      const added = page.waitForRequest((request) =>
        request.method() === "POST" && request.url().endsWith("/glossary/terms"),
      );
      await add.getByRole("button", {
        name: chinese ? "添加" : "Add",
        exact: true,
      }).click();
      expect((await added).postDataJSON()).toMatchObject({
        reading: japanese ? "あたらしい" : "",
      });
      await expect(add).toHaveCount(0);
    });
  }
}

test("detected Japanese source enables readings without reopening the details", async ({ page }) => {
  let source = "auto";
  await fakeApi(page, { [`/projects/${pid}/glossary/terms`]: [term] });
  await page.route(`**/api/projects/${pid}`, (route) =>
    route.fulfill({ json: { ...project, source_lang: source } }),
  );
  await page.goto(`/projects/${pid}/glossary`);
  await page.getByRole("button", { name: "Edit term", exact: true }).click();
  const details = page.getByRole("dialog");
  const reading = details.getByRole("textbox", { name: "Reading", exact: true });
  await expect(reading).toHaveCount(0);
  source = "ja";
  await expect(reading).toHaveValue(term.reading, { timeout: 10_000 });
  await expect(page.getByRole("table").getByRole("columnheader", { name: "Reading" })).toHaveCount(0);
});
