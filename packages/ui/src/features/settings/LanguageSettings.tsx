import { Select, SelectItem } from "../../components/ui/select";
import { locales, useI18n } from "../../i18n";
import { isLocale } from "../../i18n/catalog";
import { isThemePreference, useTheme } from "../../theme";
import { SettingsSection } from "./SettingsSection";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

export function LanguageSettings() {
  const { locale, setLocale, t } = useI18n();
  const { theme, setTheme } = useTheme();
  return (
    <SettingsSection title={t("settings.sectionGeneral")} grouped>
      <div className="overflow-hidden rounded-xl border border-border bg-card">
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
      </div>
    </SettingsSection>
  );
}
