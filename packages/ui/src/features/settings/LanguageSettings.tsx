import { Label } from "@/components/ui/form";
import { Select, SelectItem } from "@/components/ui/select";
import { locales, useI18n } from "@/i18n";
import { isLocale } from "@/i18n/catalog";
import { SettingsSection } from "./SettingsSection";

export function LanguageSettings() {
  const { locale, setLocale, t } = useI18n();
  return (
    <SettingsSection title={t("settings.sectionGeneral")}>
      <div className="max-w-xs space-y-2">
        <Label htmlFor="interface-language">{t("settings.interfaceLanguage")}</Label>
        <Select
          id="interface-language"
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
      </div>
    </SettingsSection>
  );
}
