import { useEffect, useState } from "react";
import { Button, Dialog } from "@wenyi/ui/primitives";
import { useDesktopI18n } from "./i18n";
import { notificationPermission, type NotificationPermission } from "./notifications";

/** Explain permission before asking the OS; task completion must never prompt. */
export function NotificationPermissionPrompt() {
  const { t } = useDesktopI18n();
  const [open, setOpen] = useState(false);
  const [permission, setPermission] = useState<NotificationPermission>("not_determined");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let active = true;
    void notificationPermission().then(result => {
      if (active && result === "not_determined") setOpen(true);
    }).catch(() => {});
    return () => { active = false; };
  }, []);

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
