import { expect, test } from "@playwright/test";
import { desktopDictionaries, translateDesktop } from "../src/i18n";

// translateDesktop is a pure dictionary lookup, so these checks run in the
// Playwright worker against the real dictionaries without a page or backend.
test.describe("translateDesktop locale fallback", () => {
  test("resolves the locale dictionary before English", () => {
    expect(translateDesktop("zh-CN", "notifications.title")).toBe("桌面状态通知");
    expect(translateDesktop("en", "notifications.title")).toBe("Desktop status notifications");
  });

  test("falls back to English for locales without a dictionary", () => {
    expect(translateDesktop("fr-FR", "diagnostics.title")).toBe("Diagnostics and feedback");
  });

  test("degrades a missing Chinese entry to English", () => {
    const zhCN = desktopDictionaries.zhCN as unknown as Record<string, string | undefined>;
    const original = zhCN["credentials.refresh"];
    delete zhCN["credentials.refresh"];
    try {
      expect(translateDesktop("zh-CN", "credentials.refresh")).toBe("Recheck");
    } finally {
      zhCN["credentials.refresh"] = original;
    }
  });

  test("returns the key itself when no dictionary has it", () => {
    const missingKey = "test.missingKey" as string as keyof typeof desktopDictionaries.en;
    expect(translateDesktop("zh-CN", missingKey)).toBe("test.missingKey");
    expect(translateDesktop("en", missingKey)).toBe("test.missingKey");
  });

  test("interpolates values into the resolved template", () => {
    expect(translateDesktop("en", "credentials.environmentPlaceholder", { name: "sk-fake" })).toBe(
      "Uses sk-fake",
    );
  });
});
