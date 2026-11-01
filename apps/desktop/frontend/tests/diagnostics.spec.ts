import { expect, test, type Page } from "@playwright/test";
import { fakeApi } from "../../../../packages/ui/tests/e2e/fixtures";

const origin = "http://127.0.0.1:19481";

async function setup(page: Page, failed = false) {
  await page.addInitScript(({ origin, failed }) => {
    localStorage.setItem("wenyi.locale", "en");
    const calls: { command: string; args: unknown }[] = [];
    const status = {
      version: "1.2.3", directory: "/fake-private-user/diagnostics", bytes: 1024,
      degraded: failed, dropped: 3, detailSeconds: 0, lastErrorId: "a".repeat(32),
    };
    Object.assign(window, {
      __WENYI_DESKTOP__: failed ? undefined : { apiBase: origin, token: "fake-private-token" },
      __WENYI_DESKTOP_ERROR__: failed ? "Local engine failed." : undefined,
      diagnosticCalls: calls,
      __TAURI_INTERNALS__: {
        invoke: async (command: string, args: Record<string, unknown>) => {
          calls.push({ command, args });
          if (command === "desktop_diagnostics_status") return { ...status };
          if (command === "desktop_diagnostics_detail") {
            status.detailSeconds = args.enabled ? 1800 : 0;
          }
          if (command === "desktop_diagnostics_export") return { path: "/fixture/diagnostics.zip" };
          return null;
        },
      },
    });
  }, { origin, failed });
  if (!failed) await fakeApi(page, {}, origin);
}

test("Desktop settings export a native report and temporary detail does not save configuration", async ({ page }) => {
  await setup(page);
  const writes: string[] = [];
  page.on("request", request => {
    if (["POST", "PUT", "PATCH", "DELETE"].includes(request.method())) writes.push(request.url());
  });
  await page.goto("/settings/diagnostics");
  const section = page.getByTestId("desktop-diagnostics");
  await expect(section.getByText("Stored: 0.0 MiB · Dropped records: 3")).toBeVisible();
  await section.getByRole("button", { name: "Enable detailed logging for 30 minutes" }).click();
  await expect(section.getByText("Detailed logging: 1800 seconds remaining")).toBeVisible();
  await section.getByRole("button", { name: "Stop detailed logging" }).click();
  await expect(section.getByText(/seconds remaining/)).toHaveCount(0);
  await section.getByRole("button", { name: "Export diagnostic report", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "Export diagnostic report" });
  await expect(dialog.getByText(/No project databases/)).toBeVisible();
  await dialog.getByLabel("Time range").selectOption("session");
  await dialog.getByRole("button", { name: "Choose destination and save" }).click();
  await expect(dialog.getByRole("status")).toContainText("/fixture/diagnostics.zip");
  const calls = await page.evaluate(() => (window as unknown as {
    diagnosticCalls: { command: string; args: unknown }[];
  }).diagnosticCalls);
  expect(calls).toContainEqual({ command: "desktop_diagnostics_export", args: { range: "session" } });
  expect(writes).toEqual([]);
});

test("engine startup failure can export without any loopback requests", async ({ page }) => {
  await setup(page, true);
  const requests: string[] = [];
  page.on("request", request => {
    if (request.resourceType() === "fetch") requests.push(request.url());
  });
  await page.goto("/");
  await expect(page.getByRole("alert")).toContainText("Local engine failed");
  await page.getByRole("button", { name: "Export diagnostic report", exact: true }).click();
  await page.getByRole("button", { name: "Choose destination and save" }).click();
  await expect(page.getByRole("dialog").getByRole("status")).toContainText("diagnostics.zip");
  expect(requests).toEqual([]);
});

test("frontend diagnostics never forward exception messages or objects", async ({ page }) => {
  await setup(page);
  await page.goto("/");
  await page.evaluate(() => {
    window.dispatchEvent(new ErrorEvent("error", {
      message: "fake-private-book", filename: "/fake-private-user/book",
      error: new Error("fake-private-token"),
    }));
    window.dispatchEvent(new PromiseRejectionEvent("unhandledrejection", {
      reason: { body: "fake-private-translation" }, promise: Promise.resolve(),
    }));
  });
  const calls = await page.evaluate(() => (window as unknown as {
    diagnosticCalls: { command: string; args: unknown }[];
  }).diagnosticCalls.filter(call => call.command === "desktop_diagnostics_record"));
  expect(calls).toContainEqual({
    command: "desktop_diagnostics_record", args: { event: "frontend_error" },
  });
  expect(calls).toContainEqual({
    command: "desktop_diagnostics_record", args: { event: "frontend_rejection" },
  });
  expect(JSON.stringify(calls)).not.toContain("fake-private");
});

test("canceling the native picker reports neither a saved file nor an error", async ({ page }) => {
  await setup(page, true);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__!.invoke;
    window.__TAURI_INTERNALS__!.invoke = (command, args) =>
      command === "desktop_diagnostics_export" ? Promise.resolve(null as never) : original(command, args);
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Export diagnostic report", exact: true }).click();
  await page.getByRole("button", { name: "Choose destination and save" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("button", { name: "Choose destination and save" })).toBeEnabled();
  await expect(dialog.getByRole("status")).toHaveCount(0);
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});

test("canceling an active export returns to the dialog without an error", async ({ page }) => {
  await setup(page, true);
  await page.addInitScript(() => {
    const original = window.__TAURI_INTERNALS__!.invoke;
    let cancelExport: (() => void) | undefined;
    window.__TAURI_INTERNALS__!.invoke = (command, args) => {
      if (command === "desktop_diagnostics_export") {
        return new Promise(resolve => { cancelExport = () => resolve(null as never); });
      }
      if (command === "desktop_diagnostics_cancel") {
        cancelExport?.();
        return Promise.resolve(null as never);
      }
      return original(command, args);
    };
  });
  await page.goto("/");
  await page.getByRole("button", { name: "Export diagnostic report", exact: true }).click();
  await page.getByRole("button", { name: "Choose destination and save" }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByRole("button", { name: "Cancel export", exact: true }).click();
  await expect(dialog.getByRole("button", { name: "Choose destination and save" })).toBeEnabled();
  await expect(dialog.getByRole("status")).toHaveCount(0);
  await expect(dialog.getByRole("alert")).toHaveCount(0);
});
