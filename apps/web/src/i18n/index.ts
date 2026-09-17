import { useMemo, useSyncExternalStore } from "react";
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
    const saved = window.localStorage.getItem(localeStorageKey);
    return isLocale(saved) ? saved : defaultLocale;
  } catch {
    return defaultLocale;
  }
}

let currentLocale = readLocale();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

function onStorage(event: StorageEvent) {
  if (event.key !== localeStorageKey && event.key !== null) return;
  currentLocale = readLocale();
  notify();
}

function subscribe(listener: () => void) {
  if (!listeners.size) window.addEventListener("storage", onStorage);
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
    if (!listeners.size) window.removeEventListener("storage", onStorage);
  };
}

export function setLocale(locale: Locale) {
  if (!isLocale(locale)) return;
  currentLocale = locale;
  try {
    window.localStorage.setItem(localeStorageKey, locale);
  } catch {
    // Keep the selection usable for this session when storage is unavailable.
  }
  notify();
}

/** Translate errors and other messages produced outside React components. */
export const translate: ReturnType<typeof createTranslator> = (key, values) =>
  createTranslator(currentLocale)(key, values);

export function useI18n() {
  const locale = useSyncExternalStore(
    subscribe,
    () => currentLocale,
    () => defaultLocale,
  );
  const t = useMemo(() => createTranslator(locale), [locale]);
  return { locale, setLocale, t };
}
