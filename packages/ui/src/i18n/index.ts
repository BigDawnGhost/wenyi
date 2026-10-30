import { useMemo, useSyncExternalStore } from "react";
import { platform } from "../platform";
import {
  createTranslator,
  defaultLocale,
  isLocale,
  type Locale,
} from "./catalog";

export {
  locales,
  defaultLocale,
  type Locale,
  type MessageKey,
} from "./catalog";
export const localeStorageKey = "wenyi.locale";

function readLocale(): Locale {
  try {
    const saved = platform().preferences.get(localeStorageKey);
    return isLocale(saved) ? saved : defaultLocale;
  } catch {
    return defaultLocale;
  }
}

let currentLocale: Locale | undefined;
const getLocale = () => currentLocale ??= readLocale();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());
let unsubscribe: (() => void) | undefined;

function onStorage() {
  currentLocale = readLocale();
  notify();
}

function subscribe(listener: () => void) {
  if (!listeners.size) unsubscribe = platform().preferences.subscribe(localeStorageKey, onStorage);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) unsubscribe?.();
  };
}

export function setLocale(locale: Locale) {
  if (!isLocale(locale)) return;
  currentLocale = locale;
  try {
    platform().preferences.set(localeStorageKey, locale);
  } catch {
    // Keep the selection usable for this session when storage is unavailable.
  }
  notify();
}

/** Translate errors and other messages produced outside React components. */
export const translate: ReturnType<typeof createTranslator> = (key, values) =>
  createTranslator(getLocale())(key, values);

export function useI18n() {
  const locale = useSyncExternalStore(
    subscribe,
    getLocale,
    () => defaultLocale,
  );
  const t = useMemo(() => createTranslator(locale), [locale]);
  return { locale, setLocale, t };
}
