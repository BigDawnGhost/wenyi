import type { ReactNode } from "react";
import { Monitor } from "lucide-react";
import { useI18n } from "../i18n";

// User agent only: narrow windows and touch-enabled computers remain supported.
const mobileUserAgent = /Android|iPhone|iPad|iPod|IEMobile|Opera Mini|Mobile|Tablet/i;

export function DesktopOnly({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  if (!mobileUserAgent.test(navigator.userAgent)) return children;

  return (
    <main className="flex min-h-dvh items-center justify-center bg-background p-6">
      <section aria-labelledby="desktop-only-title" className="w-full max-w-md space-y-4 text-center">
        <Monitor className="mx-auto h-10 w-10 text-muted-foreground" aria-hidden="true" />
        <h1 id="desktop-only-title" className="text-xl font-semibold">
          {t("device.computerRequired")}
        </h1>
        <p className="text-sm leading-relaxed text-muted-foreground">
          {t("device.computerRequiredDescription")}
        </p>
      </section>
    </main>
  );
}
