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
          return new Promise((resolve, reject) => Object.assign(window, {
            completeNativeUpload: resolve, failNativeUpload: reject,
          }));
        return Promise.resolve(undefined as never);
      },
    };
  });
  await fakeApi(page, {}, origin);
  await page.goto("/projects/new");
  await page.getByLabel("Project name", { exact: true }).fill("Native book");
  await expect(page.getByRole("button", { name: "Browse files" })).toBeEnabled();
}

test("cancel releases the native selection and reopening starts clean", async ({ page }) => {
  await setup(page);
  const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
  await nativeEvent(page, { kind: "drop", source: source("cancelled") });
  await expect(dialog.getByText("cancelled.docx", { exact: true })).toBeVisible();
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await expect(page).toHaveURL("/");
  await expect(page.locator("#create-project-trigger")).toBeFocused();
  await expect.poll(() => calls(page)).toContainEqual({
    command: "native_drop_release", args: { handle: "cancelled" },
  });
  expect((await calls(page)).some(call => call.command === "native_drop_upload")).toBe(false);
  await page.locator("#create-project-trigger").click();
  await expect(dialog).toBeVisible();
  await expect(page.getByLabel("Project name", { exact: true })).toBeFocused();
  await expect(page.getByLabel("Project name", { exact: true })).toHaveValue("");
  await expect(dialog.getByText("cancelled.docx", { exact: true })).toHaveCount(0);
  await expect(dialog.getByRole("button", { name: "Create project", exact: true })).toBeDisabled();
});

test("history navigation retains an uploading lease until completion without late navigation", async ({ page }) => {
  await setup(page);
  const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click();
  await page.locator("#create-project-trigger").click();
  await page.getByLabel("Project name", { exact: true }).fill("In-flight book");
  await nativeEvent(page, { kind: "drop", source: source("in-flight") });
  await dialog.getByRole("button", { name: "Create project", exact: true }).click();
  await expect.poll(async () => (await calls(page)).filter(call => call.command === "native_drop_upload").length).toBe(1);
  await page.goBack();
  await expect(page).toHaveURL("/");
  await expect(dialog).toHaveCount(0);
  expect((await calls(page)).some(call => call.command === "native_drop_release")).toBe(false);
  await page.evaluate(project => (window as unknown as {
    completeNativeUpload: (value: unknown) => void;
  }).completeNativeUpload(project), { ...project, status: "parsing" });
  await expect.poll(() => calls(page)).toContainEqual({
    command: "native_drop_release", args: { handle: "in-flight" },
  });
  await expect(page).toHaveURL("/");
  await expect(page.getByRole("heading", { name: "My projects", exact: true })).toBeVisible();
  await page.locator("#create-project-trigger").click();
  await expect(page.getByLabel("Project name", { exact: true })).toHaveValue("");
  await expect(dialog.getByText("in-flight.docx", { exact: true })).toHaveCount(0);
});

test("failed native upload keeps the form and allows retry with a fresh source grant", async ({ page }) => {
  await setup(page);
  const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
  await nativeEvent(page, { kind: "drop", source: source("retry") });
  const create = dialog.getByRole("button", { name: "Create project", exact: true });
  await create.click();
  await expect.poll(async () => (await calls(page)).filter(call => call.command === "native_drop_upload").length).toBe(1);
  await page.evaluate(() => (window as unknown as {
    failNativeUpload: (reason: string) => void;
  }).failNativeUpload("Native upload unavailable"));
  await expect(dialog.getByText("Native upload unavailable", { exact: true })).toBeVisible();
  await expect(page.getByLabel("Project name", { exact: true })).toHaveValue("Native book");
  await expect(dialog.getByText("retry.docx", { exact: true })).toBeVisible();
  await expect(create).toBeDisabled();
  await expect(dialog.getByText("Select the source file again before retrying.", { exact: true })).toBeVisible();
  expect((await calls(page)).some(call => call.command === "native_drop_release")).toBe(false);
  // Native grants are single-use even after a failed upload; re-drop to retry.
  await nativeEvent(page, { kind: "drop", source: source("retry-again") });
  await expect(dialog.getByText("retry-again.docx", { exact: true })).toBeVisible();
  await expect(create).toBeEnabled();
  await expect(dialog.getByText("Select the source file again before retrying.", { exact: true })).toHaveCount(0);
  await expect.poll(() => calls(page)).toContainEqual({
    command: "native_drop_release", args: { handle: "retry" },
  });
  await create.click();
  await expect.poll(async () => (await calls(page)).filter(call => call.command === "native_drop_upload").length).toBe(2);
  expect((await calls(page)).filter(call => call.command === "native_drop_upload").map(call => call.args.handle)).toEqual(["retry", "retry-again"]);
  await page.evaluate(project => (window as unknown as {
    completeNativeUpload: (value: unknown) => void;
  }).completeNativeUpload(project), { ...project, status: "parsing" });
  await expect(page).toHaveURL(`/projects/${pid}`);
  await expect(dialog).toHaveCount(0);
  await expect.poll(() => calls(page)).toContainEqual({
    command: "native_drop_release", args: { handle: "retry-again" },
  });
});

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

test.describe("high-DPI source selection", () => {
  test.use({ deviceScaleFactor: 2 });
  test("physical bridge coordinates accept drops at the lower right edge of the source area", async ({ page }) => {
    await setup(page);
    const zone = page.getByRole("group", { name: "Source file selection" });
    await zone.evaluate((element, source) => {
      const rect = element.getBoundingClientRect();
      const position = {
        x: (rect.right - 3) * devicePixelRatio,
        y: (rect.bottom - 3) * devicePixelRatio,
      };
      for (const kind of ["over", "drop"]) {
        window.dispatchEvent(new CustomEvent("wenyi:native-drag", {
          cancelable: true, detail: { kind, position, source: kind === "drop" ? source : undefined },
        }));
      }
    }, source("scaled"));
    await expect(zone.getByText("scaled.docx", { exact: true })).toBeVisible();
    expect(await calls(page)).toEqual([]);
  });
});

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
    const dialog = page.getByRole("dialog", { name: "Create project", exact: true });
    await expect(dialog.getByRole("button", { name: "Cancel", exact: true })).toBeDisabled();
    await expect(dialog.getByRole("button", { name: "Close", exact: true })).toBeDisabled();
    await page.keyboard.press("Escape");
    await page.mouse.click(2, 2);
    await expect(dialog).toBeVisible();
    await expect(page).toHaveURL("/projects/new");
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
