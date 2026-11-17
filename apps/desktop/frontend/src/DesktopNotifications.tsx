import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Button, Dialog, Switch } from "@wenyi/ui/primitives";
import { SettingsRow } from "@wenyi/ui/settings";
import { useDesktopI18n } from "./i18n";
import {
  notificationPermission,
  notificationPreferenceKey,
  setNotificationsEnabled,
  useNotificationPreference,
  type NotificationPermission,
} from "./notifications";

export function NotificationSettings() {
  const { t } = useDesktopI18n();
  const queryClient = useQueryClient();
  const preference = useNotificationPreference();
  const save = useMutation({
    mutationFn: setNotificationsEnabled,
    onSuccess: enabled => queryClient.setQueryData(notificationPreferenceKey, enabled),
  });

  return <SettingsRow
    label={t("notifications.setting")}
    htmlFor="status-notifications"
    description={t("notifications.settingHelp")}
    control={<Switch
      id="status-notifications"
      checked={preference.data ?? false}
      disabled={preference.data === undefined || preference.isFetching || save.isPending}
      onCheckedChange={enabled => save.mutate(enabled)}
    />}
    detail={(preference.error || save.error) ? <>
      {preference.error && <div className="flex items-center gap-3">
        <p role="alert" className="text-sm text-destructive">{t("notifications.loadFailed")}</p>
        <Button variant="outline" disabled={preference.isFetching}
          onClick={() => void preference.refetch()}>{t("notifications.retry")}</Button>
      </div>}
      {save.error && <p role="alert" className="text-sm text-destructive">
        {t("notifications.saveFailed")}
      </p>}
    </> : undefined}
  />;
}

/** Explain permission before asking the OS; task completion must never prompt. */
export function NotificationPermissionPrompt() {
  const { t } = useDesktopI18n();
  const { data: enabled } = useNotificationPreference();
  const [open, setOpen] = useState(false);
  const [permission, setPermission] = useState<NotificationPermission>("not_determined");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!enabled) {
      setOpen(false);
      return;
    }
    let active = true;
    void notificationPermission().then(result => {
      if (active && result === "not_determined") setOpen(true);
    }).catch(() => {});
    return () => { active = false; };
  }, [enabled]);

  async function allow() {
    setBusy(true);
    try {
      const result = await notificationPermission(true);
      setPermission(result);
      if (result === "granted" || result === "not_required") setOpen(false);
    } catch { setPermission("unavailable"); }
    finally { setBusy(false); }
  }

  return <Dialog open={open} onClose={() => setOpen(false)} closeDisabled={busy}
    title={t("notifications.title")} description={t("notifications.privacy")}>
    <div className="space-y-4">
      {permission === "denied" && <p role="status">{t("notifications.denied")}</p>}
      {permission === "unavailable" && <p role="alert">{t("notifications.failed")}</p>}
      <div className="flex flex-wrap gap-3">
        {permission !== "denied" && <Button disabled={busy} onClick={() => void allow()}>
          {t("notifications.allow")}
        </Button>}
        <Button variant="outline" disabled={busy} onClick={() => setOpen(false)}>
          {t(permission === "denied" ? "notifications.close" : "notifications.later")}
        </Button>
      </div>
    </div>
  </Dialog>;
}
