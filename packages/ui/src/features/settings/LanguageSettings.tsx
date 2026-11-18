import { Select, SelectItem } from "../../components/ui/select";
import { locales, useI18n } from "../../i18n";
import { isLocale } from "../../i18n/catalog";
import { isThemePreference, useTheme } from "../../theme";
import { platform } from "../../platform";
import { SettingsGroup, SettingsSection } from "./SettingsSection";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";

export function LanguageSettings() {
  const { locale, setLocale, t } = useI18n();
  const { theme, setTheme } = useTheme();
  const GeneralSettings = platform().capabilities.generalSettings;
  return (
    <SettingsSection title={t("settings.sectionGeneral")} grouped>
      <SettingsGroup title={t("settings.interface")}>
        <div className={cn(cardSurface, "overflow-hidden")}>
          <SettingsRow
            label={t("settings.interfaceLanguage")}
            htmlFor="interface-language"
            control={
              <Select
                id="interface-language"
                className={SETTINGS_CONTROL_CLASSNAME}
                value={locale}
                onValueChange={(value) => {
                  if (isLocale(value)) setLocale(value);
                }}
              >
                {Object.entries(locales).map(([code, language]) => (
                  <SelectItem key={code} value={code} lang={code}>
                    {language.name}
                  </SelectItem>
                ))}
              </Select>
            }
          />
          <SettingsRow
            label={t("settings.appearance")}
            htmlFor="appearance"
            control={
              <Select
                id="appearance"
                className={SETTINGS_CONTROL_CLASSNAME}
                value={theme}
                onValueChange={(value) => {
                  if (isThemePreference(value)) setTheme(value);
                }}
              >
                <SelectItem value="light">{t("theme.light")}</SelectItem>
                <SelectItem value="dark">{t("theme.dark")}</SelectItem>
                <SelectItem value="system">{t("theme.system")}</SelectItem>
              </Select>
            }
          />
          {GeneralSettings && <GeneralSettings />}
        </div>
      </SettingsGroup>
    </SettingsSection>
  );
}
