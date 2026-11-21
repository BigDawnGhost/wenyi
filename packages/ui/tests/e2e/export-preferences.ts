import { expect, test } from "@playwright/test";
import { configuration, fakeApi, globalConfiguration, pid, project } from "../../sandbox/fixtures";
import { chooseOption } from "./select-helper";

export function registerTests() {
  for (const bilingual of [false, true]) {
    test(`an explicit edition survives late export defaults (${bilingual})`, async ({ page }) => {
      await fakeApi(page);
      let release!: () => void;
      const held = new Promise<void>(resolve => { release = resolve; });
      await page.route(`**/api/projects/${pid}/config`, async route => {
        await held;
        return route.fulfill({ json: { ...configuration, effective: {
          ...configuration.effective, output: { bilingual: !bilingual },
        } } });
      });
      await page.goto(`/projects/${pid}/export`);
      const edition = page.getByRole("radio", { name: bilingual ? "Bilingual" : "Monolingual", exact: true });
      await edition.click();
      release();
      await expect(page.getByRole("button", { name: "Generate export" })).toBeEnabled();
      await expect(edition).toBeChecked();
    });
  }
  for (const chinese of [false, true]) {
    const language = chinese ? "Chinese" : "English";
    const aboutName = chinese ? "附加“关于此翻译”说明页" : "Include an “About this translation” page";
    const saveName = chinese ? "保存配置" : "Save configuration";
    const preferencesName = chinese ? "导出偏好" : "Export preferences";
    const orderName = chinese ? "双语顺序" : "Bilingual order";
    const styleName = chinese ? "双语排版保留原文样式" : "Preserve source formatting in bilingual output";
    const targetFirst = chinese ? "译文在前" : "Translation first";
    const sourceFirst = chinese ? "原文在前" : "Source first";

    test(`general export preferences persist without changing project settings in ${language}`, async ({ page }, testInfo) => {
      await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
      await fakeApi(page);
      let saved = structuredClone(globalConfiguration);
      let writes = 0;
      await page.route("**/api/settings", async route => {
        if (route.request().method() === "PUT") {
          const input = route.request().postDataJSON();
          writes++;
          saved = { ...saved, yaml: input.yaml, effective: JSON.parse(input.yaml), revision: saved.revision + 1 };
        }
        await route.fulfill({ json: saved });
      });
      await page.goto("/settings");
      const preferences = page.getByRole("region", { name: preferencesName, exact: true });
      const about = preferences.getByRole("switch", { name: aboutName, exact: true });
      const order = preferences.getByRole("combobox", { name: orderName, exact: true });
      const style = preferences.getByRole("switch", { name: styleName, exact: true });
      await expect(preferences.getByRole("switch")).toHaveCount(2);
      await expect(about).toBeChecked();
      await expect(order).toHaveText(targetFirst);
      await expect(style).not.toBeChecked();
      await about.uncheck();
      await chooseOption(order, sourceFirst);
      await style.check();
      await page.getByRole("link", { name: chinese ? "高级" : "Advanced", exact: true }).click();
      const yaml = page.getByRole("textbox", { name: chinese ? "高级 YAML 配置" : "Advanced YAML configuration", exact: true });
      expect(JSON.parse(await yaml.inputValue())).toMatchObject({
        ...globalConfiguration.effective,
        output: {
          about_page: false,
          bilingual_order: "source_first", bilingual_preserve_source_style: true,
        },
      });
      await yaml.fill(`${await yaml.inputValue()}\n# unvalidated draft`);
      await page.getByRole("link", { name: chinese ? "通用" : "General", exact: true }).click();
      await expect(about).toBeDisabled();
      await expect(order).toBeDisabled();
      await expect(style).toBeDisabled();
      await page.reload();
      await expect(about).toBeChecked();
      expect(writes).toBe(0);
      await about.uncheck();
      await chooseOption(order, sourceFirst);
      await style.check();
      await page.getByRole("button", { name: saveName, exact: true }).click();
      await expect(page.getByText(chinese ? "总设置已保存" : "Global settings saved", { exact: true })).toBeVisible();
      await page.reload();
      await expect(about).not.toBeChecked();
      await expect(order).toHaveText(sourceFirst);
      await expect(style).toBeChecked();
      expect(writes).toBe(1);
      await preferences.screenshot({ path: testInfo.outputPath(`general-export-preferences-${language}.png`) });
      await page.goto(`/projects/${pid}/settings`);
      await expect(page.getByRole("switch", { name: aboutName, exact: true })).toBeChecked();
      await expect(page.getByRole("combobox", { name: orderName, exact: true })).toHaveText(targetFirst);
      await expect(page.getByRole("switch", { name: styleName, exact: true })).not.toBeChecked();
    });

    test(`project export preferences preserve advanced output fields and respect read-only state in ${language}`, async ({ page }, testInfo) => {
      await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
      const initial = {
        ...configuration.effective,
        output: {
          about_page: false,
          bilingual: true,
          bilingual_order: "source_first",
          bilingual_preserve_source_style: true,
        },
      };
      let saved = { ...configuration, effective: initial, yaml: JSON.stringify(initial) };
      await fakeApi(page);
      await page.route(`**/api/projects/${pid}/config`, async route => {
        if (route.request().method() === "PUT") {
          const { yaml } = route.request().postDataJSON();
          saved = { ...saved, yaml, effective: JSON.parse(yaml) };
        }
        await route.fulfill({ json: saved });
      });
      await page.goto(`/projects/${pid}/settings`);
      const preferences = page.getByRole("region", { name: preferencesName, exact: true });
      const about = preferences.getByRole("switch", { name: aboutName, exact: true });
      const order = preferences.getByRole("combobox", { name: orderName, exact: true });
      const style = preferences.getByRole("switch", { name: styleName, exact: true });
      await expect(preferences.getByRole("switch")).toHaveCount(2);
      await expect(about).not.toBeChecked();
      await expect(order).toHaveText(sourceFirst);
      await expect(style).toBeChecked();
      await about.check();
      await chooseOption(order, targetFirst);
      await style.uncheck();
      await page.getByRole("button", { name: saveName, exact: true }).click();
      await expect(page.getByText(chinese ? "项目配置已保存" : "Project settings saved", { exact: true })).toBeVisible();
      expect(saved.effective.output).toEqual({
        ...initial.output, about_page: true,
        bilingual_order: "target_first", bilingual_preserve_source_style: false,
      });
      await page.reload();
      await expect(about).toBeChecked();
      await expect(order).toHaveText(targetFirst);
      await expect(style).not.toBeChecked();
      await preferences.screenshot({ path: testInfo.outputPath(`project-export-preferences-${language}.png`) });
      await page.route(`**/api/projects/${pid}`, route => route.fulfill({
        json: { ...project, status: "translating" },
      }));
      await page.reload();
      await expect(about).toBeDisabled();
      await expect(order).toBeDisabled();
      await expect(style).toBeDisabled();
    });

    test(`export omits all preferences in ${language}`, async ({ page }, testInfo) => {
      await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), chinese ? "zh-CN" : "en");
      await fakeApi(page);
      await page.goto(`/projects/${pid}/export`);
      await expect(page.getByRole("button", { name: chinese ? "生成导出文件" : "Generate export", exact: true })).toBeEnabled();
      await expect(page.getByRole("switch", { name: aboutName, exact: true, includeHidden: true })).toHaveCount(0);
      const advanced = page.getByText(chinese ? "排版与高级选项" : "Layout and advanced options", { exact: true });
      await expect(advanced).toHaveCount(0);
      await page.getByRole("radio", { name: chinese ? "双语版" : "Bilingual", exact: true }).check();
      await expect(advanced).toHaveCount(0);
      await expect(page.getByRole("switch", { includeHidden: true })).toHaveCount(0);
      await expect(page.getByRole("combobox", { name: orderName, exact: true, includeHidden: true })).toHaveCount(0);
      await page.screenshot({ path: testInfo.outputPath(`export-${language}.png`), fullPage: true });
    });
  }
}
