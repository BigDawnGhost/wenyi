import { Select, SelectItem } from "../../components/ui/select";
import { useI18n } from "../../i18n";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ModelSelection({
  llm,
  models,
  disabled,
  onChange,
}: {
  llm: Document;
  models: Document;
  disabled: boolean;
  onChange: (llm: Document) => void;
}) {
  const { t } = useI18n();
  const tiers = object(llm.tiers);
  const modelOptions = Object.entries(models).map(([id, raw]) => {
    const model = object(raw);
    return (
      <SelectItem key={id} value={id}>
        {id} · {String(model.provider)} / {String(model.model)}
      </SelectItem>
    );
  });
  const tierNames = [
    ["strong", t("providerSettings.qualityTier")],
    ["cheap", t("providerSettings.economyTier")],
    ["fast", t("providerSettings.fastTier")],
  ];
  return (
    <fieldset disabled={disabled} className="min-w-0 disabled:opacity-60">
      <div className="overflow-hidden rounded-xl border border-border bg-card">
        {tierNames.map(([id, label]) => (
          <SettingsRow
            key={id}
            label={label}
            htmlFor={`tier-${id}`}
            control={
              <Select
                id={`tier-${id}`}
                className={SETTINGS_CONTROL_CLASSNAME}
                value={String(tiers[id] || "")}
                onValueChange={(value) =>
                  onChange({
                    ...llm,
                    tiers: { ...tiers, [id]: value },
                  })
                }
              >
                {modelOptions}
              </Select>
            }
          />
        ))}
      </div>
    </fieldset>
  );
}
