import en from "./locales/en";
import zhCN from "./locales/zh-CN";

export type MessageKey = keyof typeof en;
export type Messages = Record<MessageKey, string>;

/** Add each interface language here, using its own name in the selector. */
export const locales = {
  en: { name: "English", messages: en },
  "zh-CN": { name: "简体中文", messages: zhCN },
} as const;

export type Locale = keyof typeof locales;
export const defaultLocale: Locale = "en";

export function isLocale(value: unknown): value is Locale {
  return typeof value === "string" && Object.hasOwn(locales, value);
}

export function createTranslator(locale: Locale) {
  return (key: MessageKey, values: Record<string, unknown> = {}): string => {
    const message = locales[locale].messages[key] ?? en[key];
    return message.replace(/\{(\w+)\}/g, (_, name: string) => {
      const value = values[name];
      return typeof value === "number"
        ? new Intl.NumberFormat(locale).format(value)
        : String(value ?? "");
    });
  };
}
