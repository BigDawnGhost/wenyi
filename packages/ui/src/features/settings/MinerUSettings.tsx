import { useI18n } from "@/i18n";
import { platform } from "@/platform";
import { LazyBoundary } from "@/routes/LazyBoundary";
import { Card, CardContent } from "@/components/ui/card";

export function MinerUSettings() {
  const { t } = useI18n();
  const NativeCredential = platform().capabilities.externalCredentials?.MinerU;
  return (
    <Card data-testid="mineru-settings">
      <CardContent className="p-5 space-y-4">
        <h2 className="font-medium">{t("settings.mineruTitle")}</h2>
        {NativeCredential ? (
          <LazyBoundary><NativeCredential /></LazyBoundary>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">{t("settings.mineruHelp")}</p>
            <p className="text-sm"><code>MINERU_API_KEY</code></p>
            <p className="text-sm">{t("settings.mineruDeployment")}</p>
          </>
        )}
      </CardContent>
    </Card>
  );
}
