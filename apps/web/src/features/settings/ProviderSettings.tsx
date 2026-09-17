import { useI18n } from "@/i18n";
import { Input, Label, Select } from "@/components/ui/form";
import { Button } from "@/components/ui/button";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ProviderSettings({
  config,
  disabled,
  kinds,
  onChange,
}: {
  config: Document;
  disabled: boolean;
  kinds: string[];
  onChange: (llm: Document) => void;
}) {
  const { t: tr } = useI18n();
  const llm = object(config.llm);
  const providers = object(llm.providers);
  const models = object(llm.models);
  const tiers = object(llm.tiers);
  const update = (group: string, id: string, patch: Document) =>
    onChange({
      ...llm,
      [group]: {
        ...object(llm[group]),
        [id]: { ...object(object(llm[group])[id]), ...patch },
      },
    });
  const add = (group: string, prefix: string, value: Document) => {
    const entries = object(llm[group]);
    let i = 1;
    while (`${prefix}${i}` in entries) i++;
    onChange({ ...llm, [group]: { ...entries, [`${prefix}${i}`]: value } });
  };
  return (
    <fieldset disabled={disabled} className="space-y-5 disabled:opacity-60">
      <div>
        <h2 className="font-medium">
          {tr("providerSettings.apiProvidersModels")}
        </h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {tr("providerSettings.settingsAreSavedPerProjectEnterThe")}
        </p>
      </div>
      {Object.entries(providers).map(([id, raw]) => {
        const provider = object(raw);
        return (
          <div key={id} className="rounded-lg border p-4 space-y-3">
            <h3 className="font-medium text-sm">
              {tr("providerSettings.connection")}
              {id}
            </h3>
            <div className="grid sm:grid-cols-2 gap-3">
              <div>
                <Label htmlFor={`provider-${id}`}>
                  {tr("providerSettings.apiProvider")}
                </Label>
                <Select
                  id={`provider-${id}`}
                  value={String(provider.kind)}
                  onChange={(e) => {
                    // Protocol-specific options cannot be carried to another adapter.
                    onChange({
                      ...llm,
                      preset: null,
                      providers: {
                        ...providers,
                        [id]: {
                          kind: e.target.value,
                          base_url: null,
                          api_key_env: null,
                        },
                      },
                      models: Object.fromEntries(
                        Object.entries(models).map(([key, value]) => [
                          key,
                          object(value).provider === id
                            ? { ...object(value), options: {} }
                            : value,
                        ]),
                      ),
                    });
                  }}
                >
                  <option value={String(provider.kind)}>
                    {String(provider.kind)}
                  </option>
                  {kinds
                    .filter((k) => k !== provider.kind && k !== "fake")
                    .map((k) => (
                      <option key={k}>{k}</option>
                    ))}
                </Select>
              </div>
              <div>
                <Label htmlFor={`url-${id}`}>
                  {tr("providerSettings.apiBaseUrl")}
                </Label>
                <Input
                  id={`url-${id}`}
                  placeholder={tr(
                    "providerSettings.leaveBlankForTheProviderDefaultUrl",
                  )}
                  value={String(provider.base_url || "")}
                  onChange={(e) =>
                    update("providers", id, {
                      base_url: e.target.value || null,
                    })
                  }
                />
              </div>
              <div>
                <Label htmlFor={`key-${id}`}>
                  {tr("providerSettings.apiKeyEnvironmentVariable")}
                </Label>
                <Input
                  id={`key-${id}`}
                  autoComplete="off"
                  placeholder={tr(
                    "providerSettings.leaveBlankForTheProviderDefaultVariable",
                  )}
                  value={String(provider.api_key_env || "")}
                  onChange={(e) =>
                    update("providers", id, {
                      api_key_env: e.target.value || null,
                    })
                  }
                />
              </div>
              <div>
                <Label htmlFor={`timeout-${id}`}>
                  {tr("providerSettings.requestTimeoutSeconds")}
                </Label>
                <Input
                  id={`timeout-${id}`}
                  type="number"
                  min={1}
                  value={Number(provider.timeout || 600)}
                  onChange={(e) =>
                    update("providers", id, { timeout: Number(e.target.value) })
                  }
                />
              </div>
            </div>
          </div>
        );
      })}
      <Button
        type="button"
        variant="outline"
        onClick={() =>
          add("providers", "provider", { kind: "openai-compatible" })
        }
      >
        {tr("providerSettings.addApiConnection")}
      </Button>
      <p className="text-sm text-muted-foreground">
        {tr("providerSettings.afterChangingProvidersChooseAModelName")}
      </p>
      {Object.entries(models).map(([id, raw]) => {
        const model = object(raw);
        return (
          <div
            key={id}
            className="grid sm:grid-cols-3 gap-3 rounded border p-3"
          >
            <div className="text-sm self-center font-medium">
              {tr("providerSettings.modelId")}
              {id}
            </div>
            <div>
              <Label htmlFor={`connection-${id}`}>
                {tr("providerSettings.apiConnection")}
              </Label>
              <Select
                id={`connection-${id}`}
                value={String(model.provider)}
                onChange={(e) =>
                  update("models", id, {
                    provider: e.target.value,
                    options: {},
                  })
                }
              >
                {Object.keys(providers).map((p) => (
                  <option key={p}>{p}</option>
                ))}
              </Select>
            </div>
            <div>
              <Label htmlFor={`model-${id}`}>{tr("common.modelName")}</Label>
              <Input
                id={`model-${id}`}
                value={String(model.model || "")}
                onChange={(e) =>
                  update("models", id, { model: e.target.value })
                }
              />
            </div>
          </div>
        );
      })}
      <Button
        type="button"
        variant="outline"
        onClick={() =>
          add("models", "model", {
            provider: Object.keys(providers)[0],
            model: "",
            options: {},
          })
        }
      >
        {tr("providerSettings.addModel")}
      </Button>
      <div className="grid sm:grid-cols-3 gap-3">
        {[
          ["strong", tr("providerSettings.qualityTier")],
          ["cheap", tr("providerSettings.economyTier")],
          ["fast", tr("providerSettings.fastTier")],
        ].map(([tier, label]) => (
          <div key={tier}>
            <Label htmlFor={`tier-${tier}`}>{label}</Label>
            <Select
              id={`tier-${tier}`}
              value={String(tiers[tier] || "")}
              onChange={(e) =>
                onChange({
                  ...llm,
                  tiers: { ...tiers, [tier]: e.target.value },
                })
              }
            >
              {Object.keys(models).map((m) => (
                <option key={m}>{m}</option>
              ))}
            </Select>
          </div>
        ))}
      </div>
      <p className="text-xs text-muted-foreground">
        {tr("providerSettings.operationSpecificModelRoutesTakePrecedenceOver")}
      </p>
    </fieldset>
  );
}
