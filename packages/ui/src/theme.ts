import { useSyncExternalStore } from "react";
import { platform } from "./platform";

export type ThemePreference = "light" | "dark" | "system";
export type ResolvedTheme = Exclude<ThemePreference, "system">;

/** Persisted next to the locale preference ("wenyi.locale"). */
export const themeStorageKey = "wenyi.theme";

const systemDarkQuery = "(prefers-color-scheme: dark)";

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "light" || value === "dark" || value === "system";
}

function readTheme(): ThemePreference {
  try {
    const saved = platform().preferences.get(themeStorageKey);
    return isThemePreference(saved) ? saved : "system";
  } catch {
    return "system";
  }
}

function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(systemDarkQuery).matches;
}

export function resolveTheme(theme: ThemePreference): ResolvedTheme {
  return theme === "system" ? (systemPrefersDark() ? "dark" : "light") : theme;
}

function applyTheme(theme: ThemePreference): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", resolveTheme(theme) === "dark");
}

let currentTheme: ThemePreference | undefined;
const getTheme = () => {
  if (currentTheme === undefined) {
    currentTheme = readTheme();
    applyTheme(currentTheme);
  }
  return currentTheme;
};

const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

// Follow the OS preference while the preference is "system".
if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
  window.matchMedia(systemDarkQuery).addEventListener("change", () => {
    if (getTheme() === "system") {
      applyTheme("system");
      notify();
    }
  });
}

/**
 * Apply the stored preference at host startup: call once the host has
 * configured the platform and before React mounts, so every route renders
 * with the saved theme instead of waiting for a settings page to mount.
 * Safe to call more than once; useTheme still owns later switches.
 */
export function initTheme(): void {
  getTheme();
}

export function setTheme(theme: ThemePreference): void {
  currentTheme = theme;
  try {
    platform().preferences.set(themeStorageKey, theme);
  } catch {
    // Keep the selection usable for this session when storage is unavailable.
  }
  applyTheme(theme);
  notify();
}

export function useTheme() {
  const theme = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    getTheme,
    (): ThemePreference => "system",
  );
  return { theme, setTheme, resolvedTheme: resolveTheme(theme) };
}
