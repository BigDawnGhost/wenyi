import { useId, useRef, useState, type FormEvent } from "react";
import { Plus, Trash2 } from "lucide-react";
import type { Capabilities, GlobalConfig } from "../../api/types";
import { Button } from "../../components/ui/button";
import { Card } from "../../components/ui/card";
import { ErrorNotice } from "../../components/ui/data";
import { Input } from "../../components/ui/form";
import { Dialog } from "../../components/ui/misc";
import { Select, SelectItem } from "../../components/ui/select";
import { useI18n } from "../../i18n";
import { toast } from "../../lib/toast";
import { platform } from "../../platform";
import { LazyBoundary } from "../../routes/LazyBoundary";
import { ModelNameField } from "./ModelNameField";
import { ModelConnectivity } from "./ModelConnectivity";
import { isModelReferenced, object, splitModelRef, type Document, type Renames } from "./registryEdits";
import { SettingsRow, SETTINGS_CONTROL_CLASSNAME } from "./SettingsRow";
import { SettingsGroupHeader } from "./SettingsSection";

export type ProviderChanges = {
  llm: Document;
  models: Renames;
  provider?: { from: string; to: string };
  revision: number;
};
type ModelRow = { key: number; original?: string; provider: string; model: string; params: Document };
const idPattern = "[a-zA-Z][a-zA-Z0-9_\\-]*";

type EditorBase = { llm: Document; id: string; creating: boolean; revision: number };

function rowsFor(llm: Document, id: string): ModelRow[] {
  return Object.entries(object(llm.models)).filter(([ref]) => splitModelRef(ref).provider === id)
    .map(([ref, raw], key) => ({ key, original: ref, ...splitModelRef(ref), params: object(raw) }));
}

export function ProviderDialog({ initial, capabilities, draftLlm, returnFocus, onClose, onSave }: {
  initial: EditorBase;
  draftLlm: Document;
  capabilities?: Capabilities;
  returnFocus: () => HTMLElement | null;
  onClose: () => void;
  onSave: (changes: ProviderChanges) => Promise<GlobalConfig>;
}) {
  const { t } = useI18n();
  const credentials = platform().capabilities.credentials;
  const CredentialField = credentials?.Field;
  const form = useId();
  const nameRef = useRef<HTMLInputElement>(null);
  const [{ llm, id, creating, revision }, setBase] = useState(initial);
  const providers = object(llm.providers);
  const models = object(llm.models);
  const [name, setName] = useState(id);
  const [provider, setProvider] = useState<Document>(() =>
    creating ? { kind: "openai-compatible" } : object(providers[id]));
  const newModel = (key: number): ModelRow => ({ key, provider: id, model: "", params: {} });
  const [rows, setRows] = useState<ModelRow[]>(() => creating
    ? [newModel(0)]
    : rowsFor(llm, id));
  const sequence = useRef(rows.length);
  const [edit, setEdit] = useState<string>();
  const [error, setError] = useState("");
  const [saveError, setSaveError] = useState<unknown>();
  const [saving, setSaving] = useState(false);
  const inFlight = useRef(false);
  const storedConnection = creating ? undefined : id;
  const otherModels = Object.fromEntries(Object.entries(models).filter(([ref]) => splitModelRef(ref).provider !== id));
  const changeModel = (key: number, patch: Partial<Pick<ModelRow, "model" | "provider" | "params">>) =>
    setRows(current => current.map(row => row.key === key ? { ...row, ...patch } : row));
  const patchProvider = (patch: Document) => setProvider(current => ({ ...current, ...patch }));
  const save = async (changes?: ProviderChanges, credential?: string) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setSaving(true);
    setSaveError(undefined);
    try {
      const saved = changes && await onSave(changes);
      if (credentials && credential !== undefined) {
        try {
          await credentials.save(name, credential);
        } catch {
          // The registry may already have committed a create or rename. Retry
          // against that saved connection without resubmitting the registry.
          if (saved) {
            const registry = object(saved.effective.llm);
            setBase({ id: name, creating: false, llm: registry, revision: saved.revision });
            setProvider(object(object(registry.providers)[name]));
            setRows(rowsFor(registry, name));
          }
          setSaveError(t("settings.credentialsFailed"));
          return;
        }
      }
      toast.success(t("settings.globalSaved"));
      onClose();
    } catch (error) {
      setSaveError(error);
    } finally {
      inFlight.current = false;
      setSaving(false);
    }
  };
  const unchanged = !creating && name === id && provider === providers[id]
    && rows.length === Object.keys(models).length - Object.keys(otherModels).length
    && rows.every(({ original, model, provider: connection, params }) =>
      original === `${connection}/${model}` && params === models[original]);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (name !== id && Object.hasOwn(providers, name)) return setError(t("registry.duplicateId"));
    const registered = { ...otherModels };
    const renames: Renames = {};
    for (const row of rows) {
      const model = row.model.trim();
      if (!model) return setError(t("providerSettings.modelRequired"));
      const ref = `${row.provider === id ? name : row.provider}/${model}`;
      if (Object.hasOwn(registered, ref)) return setError(t("providerSettings.duplicateModel", { ref }));
      registered[ref] = row.params;
      if (row.original && ref !== row.original) renames[row.original] = ref;
    }
    const connections = { ...providers };
    if (name !== id) delete connections[id];
    void save(unchanged ? undefined : {
      llm: { ...llm, preset: null,
        providers: { ...connections, [name]: provider }, models: registered },
      models: renames,
      provider: !creating && id !== name ? { from: id, to: name } : undefined,
      revision,
    }, edit);
  };
  return (
    <Dialog open title={creating ? t("providerSettings.addApiConnection") : t("providerSettings.editProvider", { id })}
      description={t("providerSettings.draftHelp")} initialFocus={nameRef} className="max-w-3xl"
      returnFocus={returnFocus} closeDisabled={saving} onClose={onClose}
      footer={<>
        {!creating && <Button variant="ghost" className="mr-auto" disabled={saving || rows.length > 0}
          aria-label={t("registry.deleteConnection", { id })} onClick={() => {
            const remaining = { ...providers };
            delete remaining[id];
            void save({
              llm: { ...llm, preset: null, providers: remaining, models: otherModels },
              models: {}, revision,
            });
          }}><Trash2 className="h-4 w-4" />{t("providerSettings.removeProvider")}</Button>}
        <Button variant="outline" disabled={saving} onClick={onClose}>{t("common.cancel")}</Button>
        <Button type="submit" form={form} disabled={saving} aria-busy={saving || undefined}>{t("providerSettings.save")}</Button>
      </>}>
      <form id={form} onSubmit={submit} onChange={() => setError("")}>
       <fieldset disabled={saving} className="min-w-0 space-y-6">
        <section aria-labelledby={`${form}-connection`} className="space-y-3">
          <SettingsGroupHeader id={`${form}-connection`} title={t("providerSettings.connections")} />
          <Card>
          <SettingsRow label={t("registry.connectionId")} description={t("registry.invalidId")} htmlFor="provider-id"
            control={<Input id="provider-id" className={SETTINGS_CONTROL_CLASSNAME}
              ref={nameRef} value={name} required pattern={idPattern}
              onChange={e => setName(e.target.value)} />} />
          <SettingsRow label={t("providerSettings.apiProvider")} htmlFor={`provider-${id}`}
            control={<Select id={`provider-${id}`} className={SETTINGS_CONTROL_CLASSNAME}
              value={String(provider.kind)} onValueChange={kind => {
              setProvider(({ timeout, max_retries, max_concurrency, quota_group }) => ({
                kind, base_url: null, api_key_env: null, timeout, max_retries, max_concurrency, quota_group,
              }));
              setRows(current => current.map(row => row.provider !== id
                ? row : { ...row, params: { ...row.params, options: {} } }));
            }}>
              {[...new Set([String(provider.kind), ...(capabilities?.providers ?? []).filter(kind => kind !== "fake")])].map(kind =>
                <SelectItem key={kind} value={kind}>{kind}</SelectItem>)}
            </Select>} />
          <SettingsRow label={t("providerSettings.apiBaseUrl")} htmlFor={`url-${id}`}
            control={<Input id={`url-${id}`} className={SETTINGS_CONTROL_CLASSNAME}
              type="url" required={provider.kind === "openai-compatible"}
              placeholder={provider.kind === "openai-compatible" ? "https://api.example.com/v1" : t("providerSettings.leaveBlankForTheProviderDefaultUrl")}
              value={String(provider.base_url ?? "")} onChange={e => patchProvider({ base_url: e.target.value || null })} />} />
          {CredentialField ? (
            <LazyBoundary>
              <CredentialField connection={id} storedConnection={storedConnection} edit={edit} onEdit={setEdit} />
            </LazyBoundary>
          ) : (
            <SettingsRow label={t("providerSettings.apiKeyEnvironmentVariable")} htmlFor={`key-${id}`}
              control={<Input id={`key-${id}`} autoComplete="off" className={SETTINGS_CONTROL_CLASSNAME}
                placeholder={t("providerSettings.leaveBlankForTheProviderDefaultVariable")}
                value={String(provider.api_key_env ?? "")}
                onChange={e => patchProvider({ api_key_env: e.target.value || null })} />} />
          )}
          {!creating && <ModelConnectivity connection={id} saved={unchanged && edit === undefined} disabled={saving} />}
          </Card>
        </section>
        <section aria-labelledby={`${form}-models`} className="space-y-3">
          <SettingsGroupHeader id={`${form}-models`} title={t("providerSettings.models")} action={
            <Button type="button" variant="outline" size="sm"
              onClick={() => setRows([...rows, newModel(sequence.current++)])}>
              <Plus className="h-4 w-4" />{t("providerSettings.addModel")}
            </Button>
          } />
          <Card className="divide-y divide-border">
          {!rows.length && <p className="px-4 py-3 text-sm text-muted-foreground">{t("connectivity.none")}</p>}
          {rows.map(row => {
            const inUse = row.original !== undefined &&
              (isModelReferenced(llm, row.original) || isModelReferenced(draftLlm, row.original));
            const connection = row.provider;
            const adapter = connection === id ? provider : object(providers[connection]);
            return (
              <div key={row.key}>
                <ModelNameField id={`name-${row.key}`} connection={connection === id ? storedConnection ?? null : connection}
                  provider={adapter}
                  credential={connection === id ? edit : undefined}
                  action={<Button type="button" variant="ghost" size="icon" disabled={inUse}
                    aria-label={t("registry.deleteModel", { name: row.model }).trim()}
                    onClick={() => setRows(rows.filter(item => item.key !== row.key))}>
                    <Trash2 className="h-4 w-4" />
                  </Button>}
                  value={row.model} onChange={model => changeModel(row.key, { model })} />
                {Object.keys(providers).some(key => key !== id) &&
                  <SettingsRow label={t("providerSettings.apiConnection")} htmlFor={`connection-${row.key}`}
                    control={<Select id={`connection-${row.key}`} className={SETTINGS_CONTROL_CLASSNAME} value={connection}
                      onValueChange={value => changeModel(row.key, { provider: value, params: { ...row.params, options: {} } })}>
                      {[id, ...Object.keys(providers).filter(key => key !== id)].map(key =>
                        <SelectItem key={key} value={key}>{key === id ? name : key}</SelectItem>)}
                    </Select>} />}
              </div>
            );
          })}
          </Card>
        </section>
        {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
       </fieldset>
      </form>
      <ErrorNotice error={saveError} />
    </Dialog>
  );
}
