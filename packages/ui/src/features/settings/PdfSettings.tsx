import { useI18n } from "../../i18n";
import { platform } from "../../platform";
import { LazyBoundary } from "../../routes/LazyBoundary";
import { MinerUSettings } from "./MinerUSettings";
import { SettingsGroup, SettingsSection } from "./SettingsSection";

export function PdfSettings({ disabled }: { disabled: boolean }) {
  const { t, locale } = useI18n();
  const plugins = platform().capabilities.plugins;
  const Plugins = plugins?.Section;

  return (
    <SettingsSection title={t("settings.sectionPdf")} grouped>
      <SettingsGroup title={t("settings.mineruTitle")}>
        <MinerUSettings />
      </SettingsGroup>
      {Plugins && (
        <SettingsGroup title={plugins.label(locale)}>
          <LazyBoundary>
            <Plugins disabled={disabled} />
          </LazyBoundary>
        </SettingsGroup>
      )}
    </SettingsSection>
  );
}
