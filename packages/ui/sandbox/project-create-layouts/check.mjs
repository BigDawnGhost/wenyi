/** Exercise the standalone files directly, without an app server or backend. */
import assert from "node:assert/strict";
import { access } from "node:fs/promises";
import { join } from "node:path";
import { test } from "node:test";
import { chromium, expect } from "@playwright/test";

const browser = await chromium.launch();

for (const [letter, filename] of [
  ["a", "a-compact.html"],
  ["b", "b-split.html"],
  ["c", "c-source-first.html"],
]) {
  await test(`layout ${letter.toUpperCase()} opens independently and stays offline`, async () => {
    const page = await browser.newPage({ viewport: { width: 1120, height: 800 } });
    const errors = [];
    const requests = [];
    page.on("pageerror", error => errors.push(error.message));
    await page.route(/^https?:/, route => {
      requests.push(route.request().url());
      return route.abort();
    });
    try {
      await page.goto(new URL(filename, import.meta.url).href);
      const dialog = page.getByRole("dialog", { name: "创建项目", exact: true });
      const name = page.getByLabel("项目名称", { exact: true });
      const style = page.getByLabel("翻译风格", { exact: true });
      const precision = page.getByRole("switch", { name: "三译合润", exact: true });
      const prepare = page.getByRole("switch", { name: "创建后执行译前准备", exact: true });
      const create = page.getByRole("button", { name: "创建项目", exact: true });
      const file = page.getByLabel("上传原文", { exact: true });
      const notice = page.locator("#notice");
      await expect(dialog).toBeVisible();
      await expect(name).toBeFocused();
      await expect(style).toHaveValue("literary");
      await expect(precision).not.toBeChecked();
      await expect(prepare).not.toBeChecked();
      await expect(create).toBeDisabled();
      if (process.env.DELTA_SCRATCH_DIR) {
        // Capture the resting layout rather than the keyboard-focus decoration.
        await name.evaluate(input => input.blur());
        await dialog.screenshot({ path: join(process.env.DELTA_SCRATCH_DIR, `layout-${letter}.png`) });
      }

      for (const viewport of [
        { width: 1024, height: 640 },
        { width: 800, height: 560 },
      ]) {
        await page.setViewportSize(viewport);
        const bounds = await dialog.boundingBox();
        assert(bounds && bounds.x >= 0 && bounds.y >= 0);
        assert(bounds.x + bounds.width <= viewport.width);
        assert(bounds.y + bounds.height <= viewport.height);
        const footer = await create.boundingBox();
        assert(footer && footer.y >= 0 && footer.y + footer.height <= viewport.height);
        assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      }
      await page.setViewportSize({ width: 1120, height: 800 });

      await file.setInputFiles({ name: "empty.txt", mimeType: "text/plain", buffer: Buffer.alloc(0) });
      await expect(notice).toContainText("非空原文文件");
      await expect(create).toBeDisabled();
      await file.setInputFiles({ name: "source.zip", mimeType: "application/zip", buffer: Buffer.from("fixture") });
      await expect(notice).toContainText("支持格式");
      await expect(create).toBeDisabled();
      await file.setInputFiles({ name: "public.sample.docx", mimeType: "application/octet-stream", buffer: Buffer.from("Public fixture.") });
      await expect(name).toHaveValue("public.sample");
      await expect(create).toBeEnabled();
      await name.fill("自定义项目");
      await file.setInputFiles({ name: "public.pdf", mimeType: "application/pdf", buffer: Buffer.from("Public PDF fixture.") });
      await expect(name).toHaveValue("自定义项目");
      await expect(page.getByLabel("PDF 解析器")).toBeVisible();
      await page.getByLabel("源语言", { exact: true }).selectOption("zh");
      await expect(create).toBeDisabled();
      await expect(notice).toContainText("源语言和目标语言不能相同");
      await page.getByLabel("源语言", { exact: true }).selectOption("auto");
      await expect(create).toBeEnabled();
      await style.selectOption("academic");
      await precision.focus();
      await page.keyboard.press("Space");
      await expect(precision).toBeChecked();
      await prepare.check();
      await create.click();
      await expect(notice).toContainText("没有创建项目或上传文件");
      await page.getByRole("button", { name: "选择文件夹", exact: true }).click();
      await expect(notice).toContainText("布局预览");

      const data = await page.evaluateHandle(() => {
        const transfer = new DataTransfer();
        transfer.items.add(new File(["Public drop fixture."], "dropped.txt", { type: "text/plain" }));
        return transfer;
      });
      await page.locator("#source-zone").dispatchEvent("drop", { dataTransfer: data });
      await data.dispose();
      await expect(page.locator("#source-title")).toHaveText("dropped.txt");
      await expect(page.getByLabel("PDF 解析器")).toBeHidden();
      await expect(name).toHaveValue("自定义项目");
      await page.keyboard.press("Escape");
      await expect(dialog).toBeHidden();
      await page.getByRole("button", { name: "重新打开预览", exact: true }).click();
      await expect(dialog).toBeVisible();
      await page.reload();
      await expect(name).toHaveValue("");
      await expect(precision).not.toBeChecked();
      await expect(style).toHaveValue("literary");
      assert.deepEqual(errors, []);
      assert.deepEqual(requests, []);
    } finally {
      await page.close();
    }
  });
}

await test("the comparison page links to all three standalone layouts", async () => {
  const page = await browser.newPage();
  try {
    await page.goto(new URL("index.html", import.meta.url).href);
    const links = await page.locator("main a").evaluateAll(nodes => nodes.map(node => node.getAttribute("href")));
    assert.deepEqual(links, ["a-compact.html", "b-split.html", "c-source-first.html"]);
    for (const href of links) await access(new URL(href, import.meta.url));
  } finally {
    await page.close();
  }
});

await browser.close();
