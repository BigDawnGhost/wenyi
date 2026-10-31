import { expect, test } from "@playwright/test";
import { fakeApi, globalConfiguration } from "./fixtures";

const releaseNotes = `# Wenyi 1.1.0

Shared bilingual preface / 公共说明

## English

English release summary.

### Improvements

- **Better updates** with \`Markdown\`.
- ~~Old behavior~~
- [x] Completed

> Keep your workspace.

| Feature | Status |
| --- | --- |
| Updates | Ready |

\`\`\`markdown
## 中文
---
\`\`\`

---

# v1.1.0 — 中文标题

## 中文

中文更新说明。

### 改进

- **更新体验**支持 \`Markdown\`。

---

[Full changelog / 完整变更记录](https://example.com/changelog)
`;

async function setup(
  page: import("@playwright/test").Page,
  mode = "automatic",
  phase = "available",
  notes = releaseNotes,
  transfer: { downloaded?: number; total?: number | null } = {},
) {
  await page.addInitScript(({ mode, phase, notes, transfer }) => {
    const status = {
      currentVersion: "1.0.0", mode, phase, version: "1.1.0",
      notes, downloaded: 0, total: 100,
      error: undefined as string | undefined,
      ...transfer,
    };
    Object.assign(window, {
      __UPDATE_COMMANDS__: [] as string[],
      __WENYI_DESKTOP__: { apiBase: `${location.origin}/api`, token: "fixture" },
      __TAURI_INTERNALS__: {
        invoke: async (command: string) => {
          if (!command.startsWith("desktop_update_")) return null;
          (window as unknown as { __UPDATE_COMMANDS__: string[] }).__UPDATE_COMMANDS__.push(command);
          if (command === "desktop_update_status") return { ...status };
          if (command === "desktop_update_check") status.phase = "available";
          if (command === "desktop_update_download") {
            status.phase = "downloading";
            status.downloaded = 50;
            setTimeout(() => { status.phase = "ready"; status.downloaded = 100; }, 1500);
          }
          if (command === "desktop_update_install") { status.error = "busy"; status.phase = "ready"; throw "busy"; }
        },
      },
    });
  }, { mode, phase, notes, transfer });
  await fakeApi(page);
  await page.goto("/settings");
}

test("release notes open separately and render only English Markdown", async ({ page }) => {
  await setup(page);
  const section = page.getByTestId("desktop-updates");
  await expect(section).not.toContainText("English release summary.");
  await expect(section.getByRole("button", { name: "Open releases" })).toHaveCount(0);
  const opener = section.getByRole("button", { name: "View release notes", exact: true });
  await opener.click();
  const dialog = page.getByRole("dialog", { name: "Release notes", exact: true });
  await expect(dialog).toContainText("English release summary.");
  await expect(dialog).not.toContainText("中文更新说明");
  await expect(dialog).not.toContainText("公共说明");
  await expect(dialog).not.toContainText("中文标题");
  await expect(dialog).not.toContainText("Full changelog");
  await expect(dialog.getByRole("heading", { name: "Improvements", exact: true })).toBeVisible();
  await expect(dialog.locator("li strong")).toHaveText("Better updates");
  await expect(dialog.locator("li code")).toHaveText("Markdown");
  await expect(dialog.locator("del")).toHaveText("Old behavior");
  await expect(dialog.getByRole("checkbox")).toBeChecked();
  await expect(dialog.getByRole("table")).toContainText("Ready");
  await expect(dialog.locator("blockquote")).toContainText("Keep your workspace.");
  await expect(dialog.locator("pre code")).toHaveText("## 中文\n---\n");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  expect(await page.evaluate(() =>
    (window as unknown as { __UPDATE_COMMANDS__: string[] }).__UPDATE_COMMANDS__.every(
      command => command === "desktop_update_status",
    ),
  )).toBe(true);
});

test("updates sit last, poll download and confirm installation with busy feedback", async ({ page }) => {
  await setup(page);
  const section = page.getByTestId("desktop-updates");
  await expect(section).toContainText("Current version: 1.0.0");
  expect(await section.evaluate(el => el.nextElementSibling === null)).toBe(true);
  await section.getByRole("button", { name: "Check for updates" }).click();
  await section.getByRole("button", { name: "Download update" }).click();
  await expect(section.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  await page.getByRole("link", { name: "Projects", exact: true }).click();
  await expect(section).toHaveCount(0);
  await page.getByRole("link", { name: "Settings", exact: true }).click();
  await section.getByRole("button", { name: "Install and restart", exact: true }).click();
  await expect(section.getByRole("alertdialog")).toBeVisible();
  await section.getByRole("button", { name: "Confirm install and restart" }).click();
  await expect(section.getByRole("alert")).toContainText("all project jobs and native exports");
});

test("update transfers show readable sizes and theme-aware progress", async ({ page }, testInfo) => {
  await setup(page, "automatic", "downloading", releaseNotes, {
    downloaded: 64 * 1024 * 1024, total: 128 * 1024 * 1024,
  });
  const section = page.getByTestId("desktop-updates");
  await expect(section).toContainText("64.0 MB / 128.0 MB");
  await expect(section).toContainText("50%");
  await expect(section.getByRole("progressbar")).toHaveAttribute("aria-valuenow", "50");
  await section.screenshot({ path: testInfo.outputPath("update-progress.png") });
  await page.evaluate(() => document.documentElement.classList.add("dark"));
  await expect(section.getByRole("progressbar").locator("div")).toHaveCSS("background-color", "rgb(250, 250, 250)");
  await section.screenshot({ path: testInfo.outputPath("update-progress-dark.png") });
});

for (const total of [null, 0]) {
  test(`unknown download size ${total} never invents a percentage`, async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await setup(page, "automatic", "downloading", releaseNotes, { downloaded: 1024, total });
    const section = page.getByTestId("desktop-updates");
    await expect(section).toContainText("1.0 KB downloaded · total size unknown");
    await expect(section.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    await expect(section.getByRole("progressbar").locator("div")).toHaveCSS("animation-name", "none");
  });
}

for (const phase of ["checking", "installing"]) {
  test(`${phase} is indeterminate rather than reusing downloaded bytes`, async ({ page }) => {
    await setup(page, "automatic", phase, releaseNotes, { downloaded: 100, total: 100 });
    const section = page.getByTestId("desktop-updates");
    await expect(section.getByRole("progressbar")).not.toHaveAttribute("aria-valuenow");
    await expect(section).not.toContainText("100%");
  });
}

for (const mode of ["manual", "unconfigured"]) {
  test(`${mode} offers notes without a release opener or native download/install`, async ({ page }) => {
    await setup(page, mode);
    const section = page.getByTestId("desktop-updates");
    await expect(section).toContainText("Automatic installation is unavailable");
    await expect(section.getByRole("button", { name: "Open releases" })).toHaveCount(0);
    await section.getByRole("button", { name: "View release notes", exact: true }).click();
    await expect(page.getByRole("dialog")).toContainText("English release summary.");
    await expect(section.getByRole("button", { name: "Download update" })).toHaveCount(0);
    await expect(section.getByRole("button", { name: "Install and restart" })).toHaveCount(0);
  });
}

test("validated YAML is still unsaved and prevents installation", async ({ page }) => {
  await setup(page, "automatic", "ready");
  await page.locator("summary").filter({ hasText: "Advanced YAML configuration" }).click();
  const yaml = page.getByRole("textbox", { name: "Advanced YAML configuration" });
  await yaml.fill((await yaml.inputValue()) + "\n# changed");
  await expect(page.getByRole("button", { name: "Install and restart", exact: true })).toBeDisabled();
  await page.getByRole("button", { name: "Validate configuration", exact: true }).click();
  await expect(page.getByRole("button", { name: "Install and restart", exact: true })).toBeDisabled();
});

test("Chinese update labels remain Desktop-only", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await setup(page);
  await expect(page.getByTestId("desktop-updates")).toContainText("桌面端更新");
  await expect(page.getByRole("button", { name: "检查更新", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "打开发布页面" })).toHaveCount(0);
  await page.getByRole("button", { name: "查看更新日志", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "更新日志", exact: true });
  await expect(dialog).toContainText("中文更新说明");
  await expect(dialog).not.toContainText("English release summary.");
  await expect(dialog).not.toContainText("Full changelog");
  await expect(dialog.getByRole("heading", { name: "改进", exact: true })).toBeVisible();
  await expect(dialog.locator("strong")).toHaveText("更新体验");
});

test("release notes follow interface language changes without another update check", async ({ page }) => {
  await setup(page);
  await page.getByLabel("Interface language", { exact: true }).click();
  await page.getByRole("option", { name: "简体中文", exact: true }).click();
  await page.getByRole("button", { name: "查看更新日志", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "更新日志", exact: true });
  await expect(dialog).toContainText("中文更新说明");
  await expect(dialog).not.toContainText("English release summary.");
  await dialog.getByRole("button", { name: "关闭", exact: true }).click();
  await page.getByLabel("界面语言", { exact: true }).click();
  await page.getByRole("option", { name: "English", exact: true }).click();
  await page.getByRole("button", { name: "View release notes", exact: true }).click();
  await expect(page.getByRole("dialog")).toContainText("English release summary.");
  await expect(page.getByRole("dialog")).not.toContainText("中文更新说明");
});

for (const [locale, label] of [
  ["en", "**EN**"],
  ["en", "eNgLiSh"],
  ["zh-CN", "简体中文"],
  ["zh-CN", "ZH-cn"],
] as const) {
  test(`${locale} recognizes ${label} headings with CRLF and no divider`, async ({ page }) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), locale);
    const chinese = locale === "zh-CN";
    const notes = [
      `## ${chinese ? "English" : label}`,
      "",
      "English section.",
      "",
      `## ${chinese ? label : "中文"}`,
      "",
      "中文分区。",
      "",
      "## Shared footer / 共用页脚",
      "",
      "Do not show the footer.",
    ].join("\r\n");
    await setup(page, "automatic", "available", notes);
    await page.getByRole("button", { name: chinese ? "查看更新日志" : "View release notes", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(chinese ? "中文分区。" : "English section.");
    await expect(dialog).not.toContainText(chinese ? "English section." : "中文分区。");
    await expect(dialog).not.toContainText("Do not show the footer.");
  });
}

test("long Chinese Markdown scrolls inside a narrow dialog with its close button visible", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 600 });
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  const paragraphs = Array.from({ length: 40 }, (_, index) => `第 ${index + 1} 项更新：改进桌面端更新体验。`);
  await setup(page, "automatic", "available", `## 中文\n\n${paragraphs.join("\n\n")}\n\n最后一项更新。`);
  await page.getByRole("button", { name: "查看更新日志", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const close = dialog.getByRole("button", { name: "关闭", exact: true });
  await expect(close).toBeInViewport();
  const bounds = await dialog.boundingBox();
  expect(bounds).not.toBeNull();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.y).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(375);
  expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(600);
  await dialog.getByText("最后一项更新。", { exact: true }).scrollIntoViewIfNeeded();
  await expect(dialog.getByText("最后一项更新。", { exact: true })).toBeInViewport();
  await expect(close).toBeInViewport();
  await close.click();
  await expect(dialog).toHaveCount(0);
});

for (const [locale, notes] of [
  ["en", "## 中文\n\n仅中文说明。"],
  ["zh-CN", "## English\n\nEnglish only."],
  ["en", "Unmarked legacy notes / 未分区的旧说明"],
  ["zh-CN", ""],
] as const) {
  test(`${locale} shows a localized empty state for ${JSON.stringify(notes)}`, async ({ page }) => {
    await page.addInitScript(locale => localStorage.setItem("wenyi.locale", locale), locale);
    await setup(page, "automatic", "available", notes);
    const chinese = locale === "zh-CN";
    await page.getByRole("button", { name: chinese ? "查看更新日志" : "View release notes", exact: true }).click();
    const dialog = page.getByRole("dialog");
    await expect(dialog).toContainText(chinese
      ? "此版本暂无中文更新日志。"
      : "No English release notes are available for this version.");
    await expect(dialog).not.toContainText("仅中文说明");
    await expect(dialog).not.toContainText("English only.");
    await expect(dialog).not.toContainText("Unmarked legacy notes");
  });
}

test("Markdown never executes HTML or loads remote images or external navigation", async ({ page }) => {
  let remoteImageRequested = false;
  await page.route("https://example.com/tracker.png", async route => {
    remoteImageRequested = true;
    await route.abort();
  });
  await setup(page, "automatic", "available", `## English

<script>window.__NOTES_EXECUTED__ = true</script>

<img src="invalid" onerror="window.__NOTES_EXECUTED__ = true">

[Unsafe](javascript:alert(1))
[Reference](https://example.com/reference)
![Image description](https://example.com/tracker.png)
`);
  await page.getByRole("button", { name: "View release notes", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("Unsafe");
  await expect(dialog).toContainText("Reference");
  await expect(dialog).toContainText("Image description");
  await expect(dialog.locator("script, img, a")).toHaveCount(0);
  expect(await page.evaluate(() =>
    (window as unknown as { __NOTES_EXECUTED__?: boolean }).__NOTES_EXECUTED__,
  )).toBeUndefined();
  expect(remoteImageRequested).toBe(false);
});

test("Chinese GFM footnotes also localize the accessible heading", async ({ page }) => {
  await page.addInitScript(() => localStorage.setItem("wenyi.locale", "zh-CN"));
  await setup(page, "automatic", "available", `## English

English note.[^en]

[^en]: English footnote.

## 中文

中文说明。[^zh]

[^zh]: 中文注释内容。
`);
  await page.getByRole("button", { name: "查看更新日志", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("中文注释内容。");
  await expect(dialog.getByRole("heading", { name: "注释", exact: true })).toHaveCount(1);
  await expect(dialog).not.toContainText("Footnotes");
  await expect(dialog).not.toContainText("English footnote.");
});

test("unsaved and pending credential inputs block installation", async ({ page }) => {
  await setup(page, "automatic", "ready");
  const connection = Object.keys(globalConfiguration.effective.llm.providers)[0];
  const status = { mode: "manual", storage: "session", available: true,
    requires_key: true, system_storage_available: false, environment: "FAKE_KEY" };
  await page.route("**/api/desktop/credentials", route => route.fulfill({ json: { [connection]: status } }));
  let finish!: () => void;
  const saved = new Promise<void>(resolve => { finish = resolve; });
  await page.route(`**/api/desktop/credentials/${connection}`, async route => {
    await saved;
    await route.fulfill({ json: status });
  });
  const install = page.getByRole("button", { name: "Install and restart", exact: true });
  await page.getByLabel("API key", { exact: true }).fill("fake-test-value");
  await expect(install).toBeDisabled();
  await page.getByRole("button", { name: "Save configuration", exact: true }).click();
  await expect(install).toBeDisabled();
  finish();
  await expect(install).toBeEnabled();
});
