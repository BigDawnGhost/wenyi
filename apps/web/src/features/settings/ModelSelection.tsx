import { Disclosure } from "@/components/ui/disclosure";
import { Label, Select } from "@/components/ui/form";
import { useI18n } from "@/i18n";
import { operationLabel } from "@/i18n/labels";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ModelSelection({
  llm,
  models,
  operations = [],
  disabled,
  onChange,
}: {
  llm: Document;
  models: Document;
  operations?: Document[];
  disabled: boolean;
  onChange: (llm: Document) => void;
}) {
  const { t } = useI18n();
  const tiers = object(llm.tiers);
  const routes = object(llm.routes);
  const modelOptions = Object.entries(models).map(([id, raw]) => {
    const model = object(raw);
    return (
      <option key={id} value={id}>
        {id} · {String(model.provider)} / {String(model.model)}
      </option>
    );
  });
  const tierNames = [
    ["strong", t("providerSettings.qualityTier")],
    ["cheap", t("providerSettings.economyTier")],
    ["fast", t("providerSettings.fastTier")],
  ];
  return (
    <fieldset disabled={disabled} className="space-y-4 disabled:opacity-60">
      <div className="grid sm:grid-cols-3 gap-3">
        {tierNames.map(([id, label]) => (
          <div key={id}>
            <Label htmlFor={`tier-${id}`}>{label}</Label>
            <Select
              id={`tier-${id}`}
              value={String(tiers[id] || "")}
              onChange={(event) =>
                onChange({
                  ...llm,
                  tiers: { ...tiers, [id]: event.target.value },
                })
              }
            >
              {modelOptions}
            </Select>
          </div>
        ))}
      </div>
      <Disclosure
        title={t("settings.operationModels")}
        summary={t("settings.routeSummary", {
          count: Object.keys(routes).length,
        })}
      >
        <p className="text-sm text-muted-foreground">
          {t("settings.operationModelsHelp")}
        </p>
        <div className="space-y-3">
          {operations.map((operation) => {
            const id = String(operation.id);
            const route = object(routes[id]);
            const value = route.model
              ? String(route.model)
              : `tier:${route.tier || operation.tier}`;
            return (
              <div
                key={id}
                className="grid gap-2 sm:grid-cols-2 sm:items-center"
              >
                <Label
                  htmlFor={`operation-${id}`}
                  className="text-sm [overflow-wrap:anywhere]"
                >
                  {operationLabel(id, t)}
                </Label>
                <Select
                  id={`operation-${id}`}
                  value={value}
                  onChange={(event) => {
                    const selected = event.target.value;
                    const next = { ...routes };
                    if (
                      selected === `tier:${operation.tier}` &&
                      (!Array.isArray(route.fallbacks) ||
                        route.fallbacks.length === 0)
                    )
                      delete next[id];
                    else
                      next[id] = {
                        ...(selected.startsWith("tier:")
                          ? { tier: selected.slice(5) }
                          : { model: selected }),
                        fallbacks: route.fallbacks || [],
                      };
                    onChange({ ...llm, routes: next });
                  }}
                >
                  {tierNames.map(([tier, label]) => (
                    <option key={tier} value={`tier:${tier}`}>
                      {label}
                    </option>
                  ))}
                  {modelOptions}
                </Select>
              </div>
            );
          })}
        </div>
      </Disclosure>
    </fieldset>
  );
}
