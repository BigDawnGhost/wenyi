import React from "react";
import { createRoot } from "react-dom/client";
import { NotificationToaster } from "../../src/components/NotificationToaster";
import { Button } from "../../src/components/ui/button";
import { toast } from "../../src/lib/toast";
import { initTheme, setTheme } from "../../src/theme";
import "@wenyi/ui/styles.css";

function Harness() {
  return (
    <main className="space-y-5 p-6">
      <div className="flex gap-3">
        {(["light", "dark", "system"] as const).map(theme => (
          <Button key={theme} onClick={() => setTheme(theme)}>Use {theme} theme</Button>
        ))}
      </div>
      <div className="flex gap-3">
        {(["success", "error", "warning", "info", "message"] as const).map(kind => (
          <Button key={kind} onClick={() => toast[kind](`${kind} notification`, {
            description: "A readable notification in the selected application theme.",
            duration: Infinity,
          })}>
            Notify {kind}
          </Button>
        ))}
      </div>
      <NotificationToaster />
    </main>
  );
}

export function mountNotificationsHarness() {
  initTheme();
  createRoot(document.getElementById("root")!).render(<Harness />);
}
