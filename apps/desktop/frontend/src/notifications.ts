import { useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useI18n } from "@wenyi/ui/i18n";

type Native = {
  invoke: <R>(command: string, args?: Record<string, unknown>) => Promise<R>;
};

function native(): Native | undefined {
  return (window as unknown as { __TAURI_INTERNALS__?: Native }).__TAURI_INTERNALS__;
}

export type NotificationPermission =
  | "not_required" | "not_determined" | "granted" | "denied" | "unavailable";

export function notificationPermission(request = false): Promise<NotificationPermission> {
  const internals = native();
  if (!internals) return Promise.resolve("not_required");
  return internals.invoke("desktop_notifications_permission", { request });
}

export const notificationPreferenceKey = ["desktopNotificationsEnabled"] as const;

async function notificationPreference(enabled?: boolean): Promise<boolean> {
  const internals = native();
  if (!internals) {
    if (enabled === undefined) return true;
    throw new Error("Native notification settings are unavailable");
  }
  const value = await internals.invoke(
    enabled === undefined ? "desktop_notifications_enabled" : "desktop_notifications_set_enabled",
    enabled === undefined ? undefined : { enabled },
  );
  if (typeof value !== "boolean") throw new Error("Invalid notification preference response");
  return value;
}

export function useNotificationPreference() {
  return useQuery({
    queryKey: notificationPreferenceKey,
    queryFn: () => notificationPreference(),
    retry: false,
    staleTime: Infinity,
  });
}

export function setNotificationsEnabled(enabled: boolean): Promise<boolean> {
  return notificationPreference(enabled);
}

/**
 * Keep native notification text in the language the interface currently uses.
 *
 * Only the locale crosses the boundary: notifications never depend on the
 * current route, the selected project or whether the window is focused.
 */
export function useNotificationLocale() {
  const { locale } = useI18n();
  useEffect(() => {
    const internals = native();
    if (!internals) return;
    // A missing native command must not affect the interface.
    void internals
      .invoke("desktop_notifications_set_locale", { locale })
      .catch(() => {});
  }, [locale]);
}
