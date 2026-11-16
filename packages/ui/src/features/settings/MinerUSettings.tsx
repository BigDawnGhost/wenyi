import { useI18n } from "../../i18n";
import { platform } from "../../platform";
import { LazyBoundary } from "../../routes/LazyBoundary";
import { Card } from "../../components/ui/card";

export function MinerUSettings() {
  const { t } = useI18n();
  const NativeCredential = platform().capabilities.externalCredentials?.MinerU;
  return (
    <Card className="space-y-4 p-4" data-testid="mineru-settings">
      {NativeCredential ? (
        <LazyBoundary><NativeCredential /></LazyBoundary>
      ) : (
        <p className="text-sm">{t("settings.mineruDeployment")}</p>
      )}
    </Card>
  );
}
