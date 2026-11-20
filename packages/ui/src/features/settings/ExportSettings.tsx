import { Switch } from "../../components/ui/form";
import { Select, SelectItem } from "../../components/ui/select";
import { useI18n } from "../../i18n";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";

export function ExportSettings({
  config,
  disabled,
  onField,
}: {
  config: Record<string, unknown>;
  disabled: boolean;
  onField: (group: string, key: string, value: unknown) => void;
}) {
  const { t } = useI18n();
  const output = (config.output || {}) as Record<string, unknown>;

  return (
    <fieldset disabled={disabled} className="min-w-0 disabled:opacity-50">
      <div className={cn(cardSurface, "overflow-hidden")}>
        <SettingsRow
          label={t("settings.bilingualOrder")}
          htmlFor="output-bilingual-order"
          control={
            <Select
              id="output-bilingual-order"
              className={SETTINGS_CONTROL_CLASSNAME}
              disabled={disabled}
              value={output.bilingual_order === "source_first" ? "source_first" : "target_first"}
              onValueChange={(value) => onField("output", "bilingual_order", value)}
            >
              <SelectItem value="target_first">{t("settings.translationFirst")}</SelectItem>
              <SelectItem value="source_first">{t("settings.sourceFirst")}</SelectItem>
            </Select>
          }
        />
        <SettingsRow
          label={t("settings.includeAnAboutThisTranslationPage")}
          htmlFor="output-about-page"
          description={t("settings.aboutPageHelp")}
          control={
            <Switch
              id="output-about-page"
              checked={output.about_page !== false}
              onCheckedChange={(checked) => onField("output", "about_page", checked)}
            />
          }
        />
        <SettingsRow
          label={t("settings.preserveSourceFormattingInBilingualOutput")}
          htmlFor="output-preserve-source-style"
          description={t("settings.preserveSourceStyleHelp")}
          control={
            <Switch
              id="output-preserve-source-style"
              checked={Boolean(output.bilingual_preserve_source_style)}
              onCheckedChange={(checked) => onField("output", "bilingual_preserve_source_style", checked)}
            />
          }
        />
      </div>
    </fieldset>
  );
}
