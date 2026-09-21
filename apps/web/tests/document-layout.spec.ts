import { expect, test, type Locator } from "@playwright/test";
import { chapter, fakeApi, pid, project } from "./fixtures";

const longTitle =
  "Lịch sử và văn hóa Việt Nam / Études sur les échanges et les traditions régionales — ".repeat(
    3,
  );
const reference = `https://example.test/archive/${"reference".repeat(35)}`;

async function expectSingleLine(locator: Locator) {
  expect(
    await locator.evaluate((element) => {
      const range = document.createRange();
      range.selectNodeContents(element);
      return new Set(
        Array.from(range.getClientRects(), (rect) => Math.round(rect.top)),
      ).size;
    }),
  ).toBe(1);
}

async function expectNoHorizontalOverflow(locator: Locator) {
  expect(
    await locator.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
}

for (const locale of ["en", "zh-CN"] as const) {
  test.describe(`document layout in ${locale}`, () => {
    test.beforeEach(async ({ page }) => {
      await page.addInitScript(
        (value) => localStorage.setItem("wenyi.locale", value),
        locale,
      );
      await fakeApi(page, {
        [`/projects/${pid}`]: { ...project, fmt: "pdf", source_lang: "vi" },
        [`/projects/${pid}/chapters`]: [
          { ...chapter, title: longTitle },
          { ...chapter, index: 1, title: "", status: "pending" },
          { ...chapter, index: 2, title: reference },
        ],
      });
    });

    test("long PDF titles leave table statuses and actions readable", async ({
      page,
    }, testInfo) => {
      await page.setViewportSize({ width: 1440, height: 960 });
      await page.goto(`/projects/${pid}`);
      const table = page.getByRole("table");
      const first = table.locator("tbody tr").first();
      await expect(first.getByRole("cell").first()).toHaveText(
        longTitle.trim(),
      );
      for (const width of [1440, 900, 390]) {
        await page.setViewportSize({ width, height: 960 });
        for (const header of await table.getByRole("columnheader").all()) {
          await expectSingleLine(header);
        }
        for (const cell of [2, 3]) {
          await expectSingleLine(
            first.getByRole("cell").nth(cell).locator("span"),
          );
        }
        await expectSingleLine(first.getByRole("link"));
        await expectNoHorizontalOverflow(first.getByRole("cell").first());
        await expectNoHorizontalOverflow(
          table.locator("tbody tr").nth(2).getByRole("cell").first(),
        );
        await expectNoHorizontalOverflow(page.getByRole("main"));
        if (width === 1440) {
          await table.screenshot({
            path: testInfo.outputPath("chapter-table.png"),
          });
        }
      }
      await expect(
        table.locator("tbody tr").nth(1).getByRole("cell").first(),
      ).toHaveText(locale === "en" ? "Untitled chapter" : "未命名章节");
    });

    test("untitled PDF sections have a label after loading", async ({
      page,
    }) => {
      const untitled = locale === "en" ? "Untitled chapter" : "未命名章节";
      await page.route(`**/api/projects/${pid}/review/1`, (route) =>
        route.fulfill({
          json: { index: 1, title: "", segments: [], review_issues: [] },
        }),
      );
      await page.goto(`/projects/${pid}/proofreading`);
      const item = page
        .getByRole("list")
        .getByRole("link", { name: new RegExp(untitled) });
      await expect(item).toBeVisible();
      await item.click();
      await expect(page.getByRole("heading", { level: 1 })).toContainText(
        untitled,
      );
    });

    test("chapter summaries keep long titles and text readable while editing", async ({
      page,
    }, testInfo) => {
      const summary =
        "本章讨论不同地区的历史、文化与交流。".repeat(24) + reference;
      const updated = `${summary}\nUpdated summary`;
      let saved = summary;
      let submitted: unknown;
      await page.route(`**/api/projects/${pid}/analysis`, (route) =>
        route.fulfill({
          json: {
            analysis: {},
            chapter_digests: [
              { index: 12, title: longTitle + reference, digest: saved },
              { index: 18, title: "", digest: "" },
            ],
          },
        }),
      );
      await page.route(`**/api/projects/${pid}/chapter-digests/12`, (route) => {
        submitted = route.request().postDataJSON();
        saved = updated;
        return route.fulfill({ json: { ok: true } });
      });
      await page.goto(`/projects/${pid}/style`);
      await page
        .getByRole("button", {
          name: locale === "en" ? "Chapter summaries" : "章节摘要",
          exact: true,
        })
        .click();
      const preview = page.getByRole("button", { name: saved, exact: true });
      await expect(preview).toBeVisible();
      for (const width of [1440, 900, 390]) {
        await page.setViewportSize({ width, height: 960 });
        await expectNoHorizontalOverflow(page.getByRole("main"));
        await expectNoHorizontalOverflow(preview);
        const title = page.getByText(longTitle + reference, { exact: true });
        await expectNoHorizontalOverflow(title);
        const titleBox = await title.boundingBox();
        const summaryBox = await preview.boundingBox();
        expect(titleBox).not.toBeNull();
        expect(summaryBox).not.toBeNull();
        if (width >= 1024) {
          expect(summaryBox!.x).toBeGreaterThan(titleBox!.x);
          expect(summaryBox!.width).toBeGreaterThan(titleBox!.width);
        } else {
          expect(summaryBox!.y).toBeGreaterThanOrEqual(
            titleBox!.y + titleBox!.height,
          );
          expect(summaryBox!.width).toBeGreaterThan(
            (width - (width >= 768 ? 240 : 0)) * 0.6,
          );
        }
        await preview.click();
        const editor = page.getByRole("textbox");
        await expect(editor).toHaveValue(summary);
        await editor.fill(updated);
        await expectNoHorizontalOverflow(page.getByRole("main"));
        await expectNoHorizontalOverflow(editor);
        await page
          .getByRole("button", {
            name: locale === "en" ? "Cancel" : "取消",
            exact: true,
          })
          .click();
        await expect(preview).toHaveText(summary);
      }
      await expect(
        page.getByText(locale === "en" ? "Untitled chapter" : "未命名章节", {
          exact: true,
        }),
      ).toBeVisible();
      await preview.click();
      await page.getByRole("textbox").fill(updated);
      await page
        .getByRole("button", {
          name: locale === "en" ? "Save summary" : "保存摘要",
          exact: true,
        })
        .click();
      await expect(
        page.getByRole("button", { name: updated, exact: true }),
      ).toBeVisible();
      expect(submitted).toEqual({ digest: updated });
      await page.screenshot({
        path: testInfo.outputPath("chapter-summaries-mobile.png"),
      });
      await page.setViewportSize({ width: 1440, height: 960 });
      await page.screenshot({
        path: testInfo.outputPath("chapter-summaries-desktop.png"),
      });
      await page.route(`**/api/projects/${pid}`, (route) =>
        route.fulfill({
          json: { ...project, fmt: "pdf", status: "translating" },
        }),
      );
      await expect(
        page.getByRole("button", { name: updated, exact: true }),
      ).toBeDisabled({ timeout: 8000 });
      await expectNoHorizontalOverflow(page.getByRole("main"));
    });

    test("long titles and bilingual paragraphs fit the reading and editing views", async ({
      page,
    }, testInfo) => {
      const source =
        `Một buổi sáng yên tĩnh bên dòng sông. `.repeat(32) + reference;
      const target = "清晨，河水从村庄旁流过。".repeat(20) + reference;
      await page.route(`**/api/projects/${pid}/review/0`, (route) =>
        route.fulfill({
          json: {
            index: 0,
            title: `${longTitle}${reference}`,
            segments: [{ index: 12, source, target, kind: "text" }],
            review_issues: [],
          },
        }),
      );
      await page.goto(`/projects/${pid}/proofreading/0`);
      const row = page.locator("#paragraph-12");
      await expect(row.getByTestId("translation-text")).toHaveText(target);
      for (const width of [1440, 900, 390]) {
        await page.setViewportSize({ width, height: 960 });
        await expectNoHorizontalOverflow(page.getByRole("main"));
        await expectNoHorizontalOverflow(
          page.getByRole("heading", { level: 1 }),
        );
        await expectNoHorizontalOverflow(row);
        const sourceBox = await row
          .locator(":scope > div")
          .first()
          .boundingBox();
        const targetBox = await row
          .locator(":scope > div")
          .nth(1)
          .boundingBox();
        expect(sourceBox).not.toBeNull();
        expect(targetBox).not.toBeNull();
        if (width >= 1024) {
          expect(targetBox!.y).toBe(sourceBox!.y);
          expect(targetBox!.width).toBeCloseTo(sourceBox!.width, 0);
          for (const label of locale === "en"
            ? ["Source", "Translation"]
            : ["原文", "译文"]) {
            await expect(
              page.getByText(label, { exact: true }).filter({ visible: true }),
            ).toHaveCount(0);
          }
        } else {
          expect(targetBox!.y).toBeGreaterThanOrEqual(
            sourceBox!.y + sourceBox!.height,
          );
          await expect(
            row.getByText(locale === "en" ? "Translation" : "译文", {
              exact: true,
            }),
          ).toBeVisible();
        }
        await row.getByRole("button").click();
        await expect(page.getByRole("menu")).toBeVisible();
        if (width === 900) {
          await page
            .getByRole("main")
            .evaluate((element) => element.scrollBy(0, -50));
          await expect(page.getByRole("menu")).toHaveCount(0);
          await row.getByRole("button").click();
        }
        await page
          .getByRole("menuitem", {
            name: locale === "en" ? "Edit translation" : "编辑译文",
            exact: true,
          })
          .click();
        const dialog = page.getByRole("dialog");
        await expect(dialog.getByRole("textbox")).toHaveValue(target);
        await expectNoHorizontalOverflow(dialog);
        await expectNoHorizontalOverflow(dialog.getByRole("textbox"));
        expect(
          await dialog
            .getByRole("textbox")
            .evaluate(
              (element) => element.scrollHeight <= element.clientHeight + 2,
            ),
        ).toBe(true);
        await dialog.screenshot({
          path: testInfo.outputPath(`editor-${width}.png`),
        });
        await page.keyboard.press("Escape");
        await expect(dialog).toHaveCount(0);
      }
    });
  });
}
