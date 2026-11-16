import type { ReactNode } from "react";
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
  footer,
}: {
  llm: Document;
  models: Document;
  disabled: boolean;
  onChange: (llm: Document) => void;
  footer?: ReactNode;
}) {
  const { t } = useI18n();
  const tiers = object(llm.tiers);
  const modelEntries = Object.entries(models).map(([id, raw]) => {
    const model = object(raw);
    return { id, provider: String(model.provider), model: String(model.model) };
  });
  const labelCounts = new Map<string, number>();
  for (const entry of modelEntries) {
    const label = `${entry.provider} / ${entry.model}`;
    labelCounts.set(label, (labelCounts.get(label) ?? 0) + 1);
  }
  const modelOptions = modelEntries.map((entry) => {
    const label = `${entry.provider} / ${entry.model}`;
    const text = (labelCounts.get(label) ?? 0) > 1 ? `${label} · ${entry.id}` : label;
    return (
      <SelectItem key={entry.id} value={entry.id}>
        {text}
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
        {tierNames.map(([id, label]) => {
          const tierValue = String(tiers[id] || "");
          const selected = modelEntries.find((entry) => entry.id === tierValue);
          return (
            <SettingsRow
              key={id}
              label={label}
              htmlFor={`tier-${id}`}
              control={
                <Select
                  id={`tier-${id}`}
                  className={SETTINGS_CONTROL_CLASSNAME}
                  value={tierValue}
                  valueLabel={selected ? `${selected.provider} / ${selected.model}` : undefined}
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
          );
        })}
        {footer && <div className="border-t border-border px-4 py-3">{footer}</div>}
      </div>
    </fieldset>
  );
}
