import { useI18n } from "@/i18n";
import { platform } from "@/platform";
import { LazyBoundary } from "@/routes/LazyBoundary";
import { SettingsSection } from "./SettingsSection";

export function MinerUSettings() {
  const { t } = useI18n();
  const NativeCredential = platform().capabilities.externalCredentials?.MinerU;
  return (
    <SettingsSection title={t("settings.mineruTitle")} data-testid="mineru-settings">
      {NativeCredential ? (
        <LazyBoundary><NativeCredential /></LazyBoundary>
      ) : (
        <p className="text-sm">{t("settings.mineruDeployment")}</p>
      )}
    </SettingsSection>
  );
}
