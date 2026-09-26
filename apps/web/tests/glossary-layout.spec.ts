import { expect, test } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";

const terms = [
  { source: "An", target: "安", reading: "An", type: "person" },
  {
    source: "An expression about the river and the changing seasons ".repeat(4),
    target: "关于河流与季节更迭的固定表达。".repeat(12),
    reading: "Một cách diễn đạt về dòng sông và những mùa thay đổi ".repeat(4),
    type: "fixed_expression",
  },
];

for (const locale of ["en", "zh-CN"] as const) {
  test(`glossary rows stay compact across type filters in ${locale}`, async ({
    page,
  }, testInfo) => {
    await page.addInitScript(
      (value) => localStorage.setItem("wenyi.locale", value),
      locale,
    );
    await fakeApi(page, { [`/projects/${pid}/glossary/conflicts`]: [] });
    await page.route(`**/api/projects/${pid}/glossary/terms**`, (route) => {
      const type = new URL(route.request().url()).searchParams.get("type");
      return route.fulfill({
        json: terms.filter((term) => !type || term.type === type),
      });
    });
    await page.goto(`/projects/${pid}/glossary`);
    const filter = page.getByRole("combobox");
    const table = page.getByRole("table");
    const rows = table.locator("tbody tr");
    for (const width of [1440, 900, 390]) {
      await page.setViewportSize({ width, height: 960 });
      await filter.selectOption("person");
      await expect(rows).toHaveCount(1);
      const baseline = await rows.first().boundingBox();
      expect(baseline).not.toBeNull();
      expect(baseline!.height).toBeLessThanOrEqual(60);

      await filter.selectOption("");
      await expect(rows).toHaveCount(2);
      for (const row of await rows.all()) {
        const bounds = await row.boundingBox();
        expect(Math.abs(bounds!.height - baseline!.height)).toBeLessThanOrEqual(
          1,
        );
        const buttons = row.getByRole("button");
        const edit = await buttons.nth(0).boundingBox();
        const remove = await buttons.nth(1).boundingBox();
        expect(edit!.y).toBe(remove!.y);
        expect(remove!.x).toBeGreaterThanOrEqual(edit!.x + edit!.width);
      }
      expect(
        await page
          .getByRole("main")
          .evaluate(
            (element) => element.scrollWidth <= element.clientWidth + 1,
          ),
      ).toBe(true);
      if (width === 1440) {
        await table.screenshot({ path: testInfo.outputPath("glossary.png") });
      }
    }

    await page.setViewportSize({ width: 1440, height: 960 });
    const expression = rows.nth(1);
    for (const [index, field] of (
      ["source", "target", "reading"] as const
    ).entries()) {
      await expect(expression.getByRole("cell").nth(index + 1)).toHaveText(
        terms[1][field].trim(),
      );
    }
    await expression
      .getByRole("button", {
        name: locale === "en" ? "Edit term" : "编辑术语",
        exact: true,
      })
      .click();
    await expect
      .poll(() =>
        page
          .getByRole("textbox")
          .evaluateAll((elements) =>
            elements.map((element) => (element as HTMLInputElement).value),
          ),
      )
      .toEqual(
        expect.arrayContaining([
          terms[1].source,
          terms[1].target,
          terms[1].reading,
        ]),
      );
  });
}
