// Desktop bridge contract tests; these do not drive OS/Wayland drag-and-drop.
import { expect, test, type Page } from "@playwright/test";
import { fakeApi, pid, project } from "./fixtures";
import type { NativeDrag } from "../src/nativeDrop";

const origin = "http://127.0.0.1:19481";

async function setup(page: Page) {
  await page.addInitScript(() => {
    window.__WENYI_DESKTOP__ = { apiBase: "http://127.0.0.1:19481", token: "fixture-token" };
    const calls: { command: string; args: Record<string, unknown> }[] = [];
    Object.assign(window, { nativeCalls: calls });
    window.__TAURI_INTERNALS__ = {
      invoke: (command, args) => {
        calls.push({ command, args });
        if (command === "native_drop_upload")
          return new Promise((resolve) => Object.assign(window, { completeNativeUpload: resolve }));
        return Promise.resolve(undefined as never);
      },
    };
  });
  await fakeApi(page, {}, origin);
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("Native book");
  await expect(page.getByRole("button", { name: "Browse files" })).toBeEnabled();
}

async function nativeEvent(page: Page, detail: NativeDrag, outside = false) {
  const zone = page.getByRole("group", { name: "Source file selection" });
  await zone.evaluate((element, { detail, outside }) => {
    const rect = element.getBoundingClientRect();
    const position = {
      x: (outside ? -10 : rect.left + 10) * devicePixelRatio,
      y: (outside ? -10 : rect.top + 10) * devicePixelRatio,
    };
    window.dispatchEvent(new CustomEvent("wenyi:native-drag", {
      cancelable: true,
      detail: { ...detail, position },
    }));
  }, { detail, outside });
}

async function calls(page: Page) {
  return page.evaluate(() => (window as unknown as {
    nativeCalls: { command: string; args: Record<string, unknown> }[];
  }).nativeCalls);
}

const source = (handle: string, name = `${handle}.docx`, size = 123) =>
  ({ kind: "native" as const, handle, name, size });

test("Desktop bridge selects without upload, highlights/leaves and preserves selection on rejection", async ({ page }) => {
  await setup(page);
  const zone = page.getByRole("group", { name: "Source file selection" });
  const release = zone.getByText("Release to select the source file.");
  await nativeEvent(page, { kind: "enter" });
  await expect(release).toBeVisible();
  await nativeEvent(page, { kind: "leave" });
  await expect(release).toHaveCount(0);
  await nativeEvent(page, { kind: "drop", source: source("first") });
  await expect(zone.getByText("first.docx", { exact: true })).toBeVisible();
  await expect(zone).toContainText("123 bytes");
  expect(await calls(page)).toEqual([]);
  for (const message of ["Select exactly one source file.", "Select a readable regular file."]) {
    await nativeEvent(page, { kind: "error", message });
    await expect(zone.getByRole("alert")).toHaveText(message);
    await expect(zone.getByText("first.docx", { exact: true })).toBeVisible();
  }
  for (const rejected of [source("empty", "empty.docx", 0), source("unsupported", "archive.zip"), source("outside")]) {
    await nativeEvent(page, { kind: "drop", source: rejected }, rejected.handle === "outside");
    await expect.poll(() => calls(page)).toContainEqual({
      command: "native_drop_release", args: { handle: rejected.handle },
    });
    await expect(zone.getByText("first.docx", { exact: true })).toBeVisible();
  }
  await page.getByLabel("Upload source", { exact: true }).setInputFiles({
    name: "browse.docx", mimeType: "application/octet-stream", buffer: Buffer.from("browse source"),
  });
  await expect(zone.getByText("browse.docx", { exact: true })).toBeVisible();
  await expect.poll(() => calls(page)).toContainEqual({
    command: "native_drop_release", args: { handle: "first" },
  });
  let browserUpload = false;
  await page.route(`${origin}/projects`, async (route) => {
    browserUpload = route.request().postData()!.includes("browse.docx");
    await route.fulfill({ status: 503, json: { detail: "Browser upload fixture" } });
  });
  await page.getByRole("button", { name: "Create project", exact: true }).click();
  await expect(page.getByText("503: Browser upload fixture")).toBeVisible();
  expect(browserUpload).toBe(true);
  expect((await calls(page)).some(call => call.command === "native_drop_upload")).toBe(false);
});

for (const mode of ["standard", "best_of_three"] as const) {
  test(`Desktop bridge uploads ${mode} on Create, refuses pending replacement and opens overview`, async ({ page }) => {
    await setup(page);
    await page.getByRole("radio", {
      name: mode === "standard" ? "Standard" : "Three drafts + synthesis",
      exact: true,
    }).check();
    const zone = page.getByRole("group", { name: "Source file selection" });
    await nativeEvent(page, { kind: "drop", source: source("original") });
    await page.getByRole("button", { name: "Create project", exact: true }).click();
    await expect(zone).toHaveAttribute("aria-disabled", "true");
    await expect.poll(() => calls(page)).toContainEqual({
      command: "native_drop_upload",
      args: {
        handle: "original",
        project: {
          name: "Native book", source_lang: "auto", target_lang: "zh",
          prepare: false, translation_mode: mode, pdf_backend: null,
        },
      },
    });
    await nativeEvent(page, { kind: "drop", source: source("pending") });
    await expect(zone.getByText("original.docx", { exact: true })).toBeVisible();
    await expect.poll(() => calls(page)).toContainEqual({
      command: "native_drop_release", args: { handle: "pending" },
    });
    await page.evaluate(project => (window as unknown as {
      completeNativeUpload: (value: unknown) => void;
    }).completeNativeUpload(project), { ...project, status: "parsing" });
    await expect(page).toHaveURL(`/projects/${pid}`);
    await expect(zone).toHaveCount(0);
    await expect.poll(() => calls(page)).toContainEqual({
      command: "native_drop_release", args: { handle: "original" },
    });
  });
}
