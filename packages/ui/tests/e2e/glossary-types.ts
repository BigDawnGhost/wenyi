import { readFileSync } from "node:fs";
import { expect, test, type Page, type TestInfo } from "@playwright/test";
import { fakeApi, pid } from "./fixtures";
import { chooseOption } from "./select-helper";

export function registerTests({ apiOrigin }: { apiOrigin?: string } = {}) {
const types = [
  { id: "person", en: "Person", "zh-CN": "人物" },
  { id: "place", en: "Place", "zh-CN": "地点" },
  { id: "organization", en: "Organization", "zh-CN": "组织" },
  { id: "term", en: "Terms", "zh-CN": "术语" },
  { id: "technique", en: "Technique", "zh-CN": "技法" },
  { id: "item", en: "Item", "zh-CN": "物品" },
  { id: "setting", en: "Setting", "zh-CN": "设定" },
  { id: "appellation", en: "Appellation", "zh-CN": "称谓" },
  { id: "honorific", en: "Honorific", "zh-CN": "敬称" },
  { id: "speech", en: "Speech habit", "zh-CN": "口癖" },
  { id: "fixed_expression", en: "Fixed expression", "zh-CN": "固定表达" },
];

async function setup(page: Page, info: TestInfo, locale: string) {
  await page.addInitScript((value) => localStorage.setItem("wenyi.locale", value), locale);
  await fakeApi(page, {
    [`/projects/${pid}/glossary/terms`]: types.map(({ id }) => ({
      source: `Source ${id}`,
      target: `Translation ${id}`,
      type: id,
      reading: "",
      note: "",
      gender: "",
      aliases: [],
    })),
  }, apiOrigin);
  await page.route(`**/projects/${pid}/glossary/terms{,/*}`, async (route) => {
    if (["POST", "PUT"].includes(route.request().method())) {
      await route.fulfill({ json: route.request().postDataJSON() });
    } else {
      await route.fallback();
    }
  });
  await page.goto(`/projects/${pid}/glossary`);
}

test("glossary type coverage matches the extraction schema", () => {
  const prompt = readFileSync(new URL(
    "../../../core/wenyi_core/i18n/data/tasks/glossary_extractor_system.txt",
    import.meta.url,
  ), "utf8");
  const schemaTypes = prompt.match(/"type":"([^"]+)"/)?.[1].split("|");
  expect(schemaTypes?.sort()).toEqual(types.map(({ id }) => id).sort());
});

for (const locale of ["en", "zh-CN"] as const) {
  const chinese = locale === "zh-CN";
  const typeLabel = chinese ? "类型" : "Type";
  const labels = types.map((type) => type[locale]);

  test(`glossary localizes every type and filters by English IDs in ${locale}`, async ({
    page,
  }, info) => {
    await setup(page, info, locale);
    const rows = page.getByRole("table").locator("tbody tr");
    await expect(rows).toHaveCount(types.length);
    for (const type of types) {
      const row = rows.filter({ hasText: `Source ${type.id}` });
      await expect(row.getByRole("cell").nth(3)).toHaveText(type[locale]);
    }
    const filter = page.getByRole("combobox", { name: typeLabel, exact: true });
    await filter.click();
    await expect(page.getByRole("option")).toHaveText([
      chinese ? "全部类型" : "All types",
      ...labels,
    ]);
    await page.keyboard.press("Escape");
    for (const type of types) {
      await chooseOption(filter, type[locale]);
      await expect(page).toHaveURL(new RegExp(`\\?type=${type.id}$`));
      await expect(rows).toHaveCount(1);
      await expect(rows.getByRole("cell").nth(1)).toHaveText(`Source ${type.id}`);
      await expect(rows.getByRole("cell").nth(3)).toHaveText(type[locale]);
    }
    await page.reload();
    await expect(filter).toHaveText(types.at(-1)![locale]);
    await expect(rows).toHaveCount(1);
  });

  for (const type of types) {
    test(`glossary add and edit keep the ${type.id} ID in ${locale}`, async ({
      page,
    }, info) => {
      await setup(page, info, locale);
      await page.getByRole("button", {
        name: chinese ? "添加术语" : "Add term", exact: true,
      }).click();
      const add = page.getByRole("dialog");
      const addType = add.getByRole("combobox", { name: typeLabel, exact: true });
      await addType.click();
      await expect(page.getByRole("option")).toHaveText(labels);
      await page.keyboard.press("Escape");
      await chooseOption(addType, type[locale]);
      await add.getByRole("textbox").nth(0).fill(`New ${type.id}`);
      await add.getByRole("textbox").nth(1).fill(`New translation ${type.id}`);
      const added = page.waitForRequest((request) =>
        request.method() === "POST" && request.url().endsWith("/glossary/terms"),
      );
      await add.getByRole("button", { name: chinese ? "添加" : "Add", exact: true }).click();
      expect((await added).postDataJSON()).toMatchObject({ type: type.id });
      await expect(add).toHaveCount(0);

      const row = page.getByRole("table").locator("tbody tr").filter({
        hasText: `Source ${type.id}`,
      });
      await row.getByRole("button", {
        name: chinese ? "编辑术语" : "Edit term", exact: true,
      }).click();
      const edit = page.getByRole("dialog");
      const editType = edit.getByRole("combobox", { name: typeLabel, exact: true });
      await expect(editType).toHaveText(type[locale]);
      await editType.click();
      await expect(page.getByRole("option")).toHaveText(labels);
      await page.keyboard.press("Escape");
      await chooseOption(editType, type[locale]);
      const saved = page.waitForRequest((request) => request.method() === "PUT");
      await edit.getByRole("button", { name: chinese ? "保存" : "Save", exact: true }).click();
      expect((await saved).postDataJSON()).toMatchObject({ type: type.id });
      await expect(edit).toHaveCount(0);
    });
  }
}
}
