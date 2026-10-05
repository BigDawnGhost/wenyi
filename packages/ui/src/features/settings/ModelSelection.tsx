import { Disclosure } from "@/components/ui/disclosure";
import { Label } from "@/components/ui/form";
import { Select, SelectItem } from "@/components/ui/select";
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
    <fieldset disabled={disabled} className="min-w-0 space-y-4 disabled:opacity-60">
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
        {tierNames.map(([id, label]) => (
          <div key={id} className="min-w-0">
            <Label htmlFor={`tier-${id}`}>{label}</Label>
            <Select
              id={`tier-${id}`}
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
                  onValueChange={(selected) => {
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
                    <SelectItem key={tier} value={`tier:${tier}`}>
                      {label}
                    </SelectItem>
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
