import { Select, SelectItem } from "../../components/ui/select";
import { locales, useI18n } from "../../i18n";
import { isLocale } from "../../i18n/catalog";
import { SettingsSection } from "./SettingsSection";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

export function LanguageSettings() {
  const { locale, setLocale, t } = useI18n();
  return (
    <SettingsSection title={t("settings.sectionGeneral")}>
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
      </div>
    </SettingsSection>
  );
}
