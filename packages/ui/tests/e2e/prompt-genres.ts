import { expect, test, type Route } from "@playwright/test";
import { configuration, fakeApi, pid, project } from "../../sandbox/fixtures";
import { chooseOption } from "./select-helper";

/** Creation uses host upload adapters; both hosts must retain the prompt-family choice. */
export const desktopHostSensitive = true;

async function metadata(route: Route) {
  const form = await new Response(new Uint8Array(route.request().postDataBuffer()!), {
    headers: { "content-type": route.request().headers()["content-type"] },
  }).formData();
  return JSON.parse(String(form.get("project"))) as Record<string, unknown>;
}

export function registerTests() {
  for (const chinese of [false, true]) {
    for (const precision of [false, true]) {
      test(`academic creation persists in ${chinese ? "Chinese" : "English"} with ${precision ? "precision" : "standard"} translation`, async ({ page }) => {
        await page.addInitScript(
          locale => localStorage.setItem("wenyi.locale", locale),
          chinese ? "zh-CN" : "en",
        );
        const selectedConfig = {
          ...configuration,
          effective: {
            ...configuration.effective,
            pipeline: {
              ...configuration.effective.pipeline,
              prompt_genre: "academic",
              translation_mode: precision ? "three_draft_synthesis" : "standard",
            },
          },
        };
        await fakeApi(page, { [`/projects/${pid}/config`]: selectedConfig });
        let submitted: Record<string, unknown> | undefined;
        await page.route("**/projects", async route => {
          if (route.request().method() !== "POST") return route.fallback();
          submitted = await metadata(route);
          await route.fulfill({ status: 201, json: project });
        });
        await page.goto("/projects/new");
        const style = page.getByLabel(chinese ? "翻译风格" : "Translation style", { exact: true });
        await expect(style).toHaveText(chinese ? "文学" : "Literary");
        await style.press("Enter");
        await expect(page.getByRole("option", {
          name: chinese ? "文学" : "Literary", exact: true,
        })).toBeFocused();
        await page.keyboard.press("End");
        await expect(page.getByRole("option", {
          name: chinese ? "学术" : "Academic", exact: true,
        })).toBeFocused();
        await page.keyboard.press("Enter");
        await expect(style).toHaveText(chinese ? "学术" : "Academic");
        const precisionSwitch = page.getByRole("switch", {
          name: chinese ? "三译合润" : "Three drafts + synthesis", exact: true,
        });
        await expect(precisionSwitch).not.toBeChecked();
        await expect(page.getByRole("radio")).toHaveCount(0);
        await precisionSwitch.focus();
        await page.keyboard.press("Space");
        await expect(precisionSwitch).toBeChecked();
        await page.keyboard.press("Enter");
        await expect(precisionSwitch).not.toBeChecked();
        if (precision) {
          await precisionSwitch.check();
        }
        await page.getByLabel(chinese ? "上传原文" : "Upload source", { exact: true }).setInputFiles({
          name: "academic.txt", mimeType: "text/plain", buffer: Buffer.from("A public test argument."),
        });
        await page.getByRole("button", {
          name: chinese ? "创建项目" : "Create project", exact: true,
        }).click();
        await expect(page).toHaveURL(`/projects/${pid}`);
        expect(submitted?.prompt_genre).toBe("academic");
        expect(submitted?.translation_mode).toBe(precision ? "three_draft_synthesis" : "standard");
        await page.goto(`/projects/${pid}/settings`);
        const savedStyle = page.getByRole("textbox", {
          name: chinese ? "翻译风格" : "Translation style", exact: true,
        });
        await expect(savedStyle).toHaveValue(chinese ? "学术" : "Academic");
        await expect(savedStyle).toHaveAttribute("readonly");
        await page.reload();
        await expect(savedStyle).toHaveValue(chinese ? "学术" : "Academic");
      });
    }
  }

  test("literary stays the default after cancelling an academic project", async ({ page }) => {
    await fakeApi(page);
    let submitted: Record<string, unknown> | undefined;
    await page.route("**/projects", async route => {
      if (route.request().method() !== "POST") return route.fallback();
      submitted = await metadata(route);
      await route.fulfill({ json: project });
    });
    await page.goto("/projects/new");
    const style = page.getByLabel("Translation style", { exact: true });
    await chooseOption(style, "Academic");
    const precisionSwitch = page.getByRole("switch", { name: "Three drafts + synthesis", exact: true });
    await precisionSwitch.check();
    await page.getByRole("button", { name: "Cancel", exact: true }).click();
    await page.locator("#create-project-trigger").click();
    await expect(style).toHaveText("Literary");
    await expect(precisionSwitch).not.toBeChecked();
    await page.getByLabel("Upload source", { exact: true }).setInputFiles({
      name: "book.txt", mimeType: "text/plain", buffer: Buffer.from("A public test story."),
    });
    await page.getByRole("button", { name: "Create project", exact: true }).click();
    await expect(page).toHaveURL(`/projects/${pid}`);
    expect(submitted?.prompt_genre).toBe("literary");
    expect(submitted?.translation_mode).toBe("standard");
  });

  for (const genre of [undefined, "academic"]) {
    test(`existing ${genre || "legacy literary"} project shows a locked style`, async ({ page }) => {
      await fakeApi(page, {
        [`/projects/${pid}/config`]: {
          ...configuration,
          effective: {
            ...configuration.effective,
            pipeline: {
              ...configuration.effective.pipeline,
              prompt_genre: genre,
              translation_mode: genre ? "three_draft_synthesis" : "standard",
            },
          },
        },
      });
      await page.goto(`/projects/new?project=${pid}`);
      const style = page.getByLabel("Translation style", { exact: true });
      await expect(style).toHaveText(genre ? "Academic" : "Literary");
      await expect(style).toBeDisabled();
      const precisionSwitch = page.getByRole("switch", { name: "Three drafts + synthesis", exact: true });
      await expect(precisionSwitch).toBeChecked({ checked: !!genre });
      await expect(precisionSwitch).toBeDisabled();
    });
  }
}
