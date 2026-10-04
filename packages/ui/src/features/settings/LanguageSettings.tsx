import { Card, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/form";
import { Select, SelectItem } from "@/components/ui/select";
import { locales, useI18n } from "@/i18n";
import { isLocale } from "@/i18n/catalog";

export function LanguageSettings() {
  const { locale, setLocale, t } = useI18n();
  return (
    <Card>
      <CardContent className="p-5 space-y-3">
        <Label htmlFor="interface-language">
          {t("settings.interfaceLanguage")}
        </Label>
        <p className="text-sm text-muted-foreground">
          {t("settings.languageDescription")}
        </p>
        <Select
          id="interface-language"
          value={locale}
          className="max-w-xs"
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
        <p className="text-xs text-muted-foreground">
          {t("settings.savedLocally")}
        </p>
      </CardContent>
    </Card>
  );
}
