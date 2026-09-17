import { useCallback, useEffect, useState } from "react";
import { Trash2 } from "lucide-react";
import { RegistryIdField } from "./RegistryIdField";
import { registryReferences, type RegistryGroup } from "./registryEdits";
import { useI18n } from "@/i18n";
import { Input, Label, Select } from "@/components/ui/form";
import { Disclosure } from "@/components/ui/disclosure";
import { Button } from "@/components/ui/button";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ProviderSettings({
  config,
  disabled,
  kinds,
  error,
  onChange,
  onRename,
  onEditingChange,
}: {
  config: Document;
  disabled: boolean;
  kinds: string[];
  error?: unknown;
  onChange: (llm: Document) => void;
  onRename: (group: RegistryGroup, oldId: string, newId: string) => void;
  onEditingChange: (pending: boolean) => void;
}) {
  const { t: tr } = useI18n();
  const [pendingIds, setPendingIds] = useState<Record<string, boolean>>({});
  const onPending = useCallback((key: string, pending: boolean) => {
    setPendingIds((current) => {
      if (!!current[key] === pending) return current;
      const next = { ...current };
      if (pending) next[key] = true;
      else delete next[key];
      return next;
    });
  }, []);
  useEffect(
    () => onEditingChange(Object.keys(pendingIds).length > 0),
    [pendingIds, onEditingChange],
  );
  const llm = object(config.llm);
  const providers = object(llm.providers);
  const models = object(llm.models);
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
  const remove = (group: RegistryGroup, id: string) => {
    const entries = { ...object(llm[group]) };
    delete entries[id];
    onChange({ ...llm, preset: null, [group]: entries });
  };
  const entryHeader = (group: RegistryGroup, id: string) => {
    const references = registryReferences(llm, group, id);
    const descriptionId = `references-${group}-${id}`;
    return (
      <div className="space-y-2">
        <RegistryIdField
          group={group}
          id={id}
          ids={Object.keys(object(llm[group]))}
          onRename={onRename}
          onPending={onPending}
          action={
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
              disabled={references.length > 0}
              aria-describedby={references.length ? descriptionId : undefined}
              aria-label={tr(
                group === "providers"
                  ? "registry.deleteConnection"
                  : "registry.deleteModel",
                { id },
              )}
              onClick={() => remove(group, id)}
            >
              <Trash2 className="h-4 w-4" />
              {tr("registry.delete")}
            </Button>
          }
        />
        {references.length > 0 && (
          <p
            id={descriptionId}
            className="text-xs text-muted-foreground [overflow-wrap:anywhere]"
          >
            {tr("registry.usedBy", { references: references.join(", ") })}
          </p>
        )}
      </div>
    );
  };
  return (
    <fieldset disabled={disabled} className="space-y-5 disabled:opacity-60">
      <div>
        <h2 className="font-medium">{tr("settings.registeredModels")}</h2>
        <p className="mt-2 text-sm text-muted-foreground">
          {llm.preset
            ? tr("settings.presetSummary", { name: String(llm.preset) })
            : tr("settings.custom")}
        </p>
      </div>
      <Disclosure
        title={tr("providerSettings.apiProvidersModels")}
        error={error}
        summary={tr("settings.modelSummary", {
          providers: Object.keys(providers).length,
          models: Object.keys(models).length,
          routes: Object.keys(object(llm.routes)).length,
        })}
      >
        <p className="text-sm text-muted-foreground">
          {tr("providerSettings.sharedRegistryHelp")}
        </p>
        {Object.entries(providers).map(([id, raw]) => {
          const provider = object(raw);
          return (
            <div key={id} className="rounded-lg border p-4 space-y-3">
              {entryHeader("providers", id)}
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
                    value={Number(provider.timeout ?? 600)}
                    onChange={(e) =>
                      update("providers", id, {
                        timeout: Number(e.target.value),
                      })
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
            <div key={id} className="rounded-lg border p-4 space-y-3">
              {entryHeader("models", id)}
              <div className="grid sm:grid-cols-2 gap-3">
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
                  <Label htmlFor={`model-${id}`}>
                    {tr("common.modelName")}
                  </Label>
                  <Input
                    id={`model-${id}`}
                    value={String(model.model || "")}
                    onChange={(e) =>
                      update("models", id, { model: e.target.value })
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
          disabled={Object.keys(providers).length === 0}
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
        <p className="text-xs text-muted-foreground">
          {tr(
            "providerSettings.operationSpecificModelRoutesTakePrecedenceOver",
          )}
        </p>
      </Disclosure>
    </fieldset>
  );
}
