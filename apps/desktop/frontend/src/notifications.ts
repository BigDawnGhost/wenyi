import { useEffect } from "react";
import { useI18n } from "@wenyi/ui/i18n";

type Native = {
  invoke: <R>(command: string, args?: Record<string, unknown>) => Promise<R>;
};

function native(): Native | undefined {
  return (window as unknown as { __TAURI_INTERNALS__?: Native }).__TAURI_INTERNALS__;
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
