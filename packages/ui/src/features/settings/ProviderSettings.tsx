import { useCallback, useEffect, useState } from "react";
import { Plus, Trash2 } from "lucide-react";
import { RegistryIdField } from "./RegistryIdField";
import { registryReferences, type RegistryGroup } from "./registryEdits";
import { SettingsGroupHeader } from "./SettingsSection";
import { useI18n } from "@/i18n";
import { Input, Label } from "@/components/ui/form";
import { Select, SelectItem } from "@/components/ui/select";
import { Button } from "@/components/ui/button";
import { platform } from "@/platform";
import { LazyBoundary } from "@/routes/LazyBoundary";
import { ModelConnectivity } from "./ModelConnectivity";
import { ModelNameField } from "./ModelNameField";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function ProviderSettings({
  config,
  disabled,
  kinds,
  onChange,
  onRename,
  onEditingChange,
  saved = false,
}: {
  config: Document;
  disabled: boolean;
  kinds: string[];
  onChange: (llm: Document) => void;
  onRename: (group: RegistryGroup, oldId: string, newId: string) => void;
  onEditingChange: (pending: boolean) => void;
  saved?: boolean;
}) {
  const { t: tr } = useI18n();
  const CredentialField = platform().capabilities.credentials?.Field;
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
  const idField = (group: RegistryGroup, id: string) => {
    const references = registryReferences(llm, group, id);
    const noteId = `references-${group}-${id}`;
    return (
      <RegistryIdField
        group={group}
        id={id}
        ids={Object.keys(object(llm[group]))}
        onRename={onRename}
        onPending={onPending}
        // The note only appears when it explains why deletion is unavailable.
        note={
          references.length
            ? { id: noteId, text: tr("registry.usedBy", { references: references.join(", ") }) }
            : undefined
        }
        action={
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className="shrink-0 text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
            disabled={references.length > 0}
            aria-describedby={references.length ? noteId : undefined}
            aria-label={tr(
              group === "providers" ? "registry.deleteConnection" : "registry.deleteModel",
              { id },
            )}
            title={tr(
              group === "providers" ? "registry.deleteConnection" : "registry.deleteModel",
              { id },
            )}
            onClick={() => remove(group, id)}
          >
            <Trash2 className="h-4 w-4" aria-hidden="true" />
          </Button>
        }
      />
    );
  };
  return (
    <fieldset disabled={disabled} className="min-w-0 space-y-6 disabled:opacity-60">
      <div className="space-y-3">
        <SettingsGroupHeader
          title={tr("providerSettings.connections")}
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => add("providers", "provider", { kind: "openai-compatible" })}
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              {tr("providerSettings.addApiConnection")}
            </Button>
          }
        />
        {Object.entries(providers).map(([id, raw]) => {
          const provider = object(raw);
          const environmentField = (
            <div className="min-w-0 space-y-2">
              <Label htmlFor={`key-${id}`}>
                {tr("providerSettings.apiKeyEnvironmentVariable")}
              </Label>
              <Input
                id={`key-${id}`}
                autoComplete="off"
                placeholder={tr("providerSettings.leaveBlankForTheProviderDefaultVariable")}
                value={String(provider.api_key_env || "")}
                onChange={(e) =>
                  update("providers", id, { api_key_env: e.target.value || null })
                }
              />
            </div>
          );
          return (
            <div key={id} className="space-y-4 rounded-lg border p-4">
              <div className="grid grid-cols-2 gap-4">
                {idField("providers", id)}
                <div className="min-w-0 space-y-2">
                  <Label htmlFor={`provider-${id}`}>{tr("providerSettings.apiProvider")}</Label>
                  <Select
                    id={`provider-${id}`}
                    value={String(provider.kind)}
                    onValueChange={(value) => {
                      // Protocol-specific options cannot be carried to another adapter.
                      onChange({
                        ...llm,
                        preset: null,
                        providers: {
                          ...providers,
                          [id]: { kind: value, base_url: null, api_key_env: null },
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
                    <SelectItem value={String(provider.kind)}>{String(provider.kind)}</SelectItem>
                    {kinds
                      .filter((k) => k !== provider.kind && k !== "fake")
                      .map((k) => (
                        <SelectItem key={k} value={k}>{k}</SelectItem>
                      ))}
                  </Select>
                </div>
                <div className="min-w-0 space-y-2">
                  <Label htmlFor={`url-${id}`}>{tr("providerSettings.apiBaseUrl")}</Label>
                  <Input
                    id={`url-${id}`}
                    placeholder={tr("providerSettings.leaveBlankForTheProviderDefaultUrl")}
                    value={String(provider.base_url || "")}
                    onChange={(e) =>
                      update("providers", id, { base_url: e.target.value || null })
                    }
                  />
                </div>
                <div className="min-w-0 space-y-2">
                  <Label htmlFor={`timeout-${id}`}>
                    {tr("providerSettings.requestTimeoutSeconds")}
                  </Label>
                  <Input
                    id={`timeout-${id}`}
                    type="number"
                    min={1}
                    value={Number(provider.timeout ?? 600)}
                    onChange={(e) =>
                      update("providers", id, { timeout: Number(e.target.value) })
                    }
                  />
                </div>
              </div>
              {CredentialField ? (
                // The credential UI may be a lazy platform chunk; keep its slot
                // filled with the environment field instead of blanking it.
                <LazyBoundary fallback={environmentField}>
                  <CredentialField connection={id}>{environmentField}</CredentialField>
                </LazyBoundary>
              ) : (
                environmentField
              )}
            </div>
          );
        })}
      </div>
      <div className="space-y-3">
        <SettingsGroupHeader
          title={tr("providerSettings.models")}
          action={
            <Button
              type="button"
              variant="outline"
              size="sm"
              disabled={Object.keys(providers).length === 0}
              onClick={() =>
                add("models", "model", {
                  provider: Object.keys(providers)[0],
                  model: "",
                  options: {},
                })
              }
            >
              <Plus className="h-4 w-4" aria-hidden="true" />
              {tr("providerSettings.addModel")}
            </Button>
          }
        />
        {Object.entries(models).map(([id, raw]) => {
          const model = object(raw);
          return (
            <div key={id} className="grid grid-cols-3 gap-4 rounded-lg border p-4">
              {idField("models", id)}
              <div className="min-w-0 space-y-2">
                <Label htmlFor={`connection-${id}`}>{tr("providerSettings.apiConnection")}</Label>
                <Select
                  id={`connection-${id}`}
                  value={String(model.provider)}
                  onValueChange={(value) =>
                    update("models", id, { provider: value, options: {} })
                  }
                >
                  {Object.keys(providers).map((p) => (
                    <SelectItem key={p} value={p}>{p}</SelectItem>
                  ))}
                </Select>
              </div>
              <ModelNameField
                id={`model-${id}`}
                value={String(model.model || "")}
                connection={String(model.provider)}
                provider={
                  model.provider && String(model.provider) in providers
                    ? object(providers[String(model.provider)])
                    : undefined
                }
                onChange={(value) => update("models", id, { model: value })}
              />
            </div>
          );
        })}
      </div>
      <ModelConnectivity saved={saved} disabled={disabled} />
    </fieldset>
  );
}
