import { useEffect, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { api } from "../../api/client";
import type { GlobalConfig } from "../../api/types";
import { platform } from "../../platform";
import { LazyBoundary } from "../../routes/LazyBoundary";
import type { SettingsSectionId } from "../../routes/manifest";
import { useI18n } from "../../i18n";
import { Button } from "../../components/ui/button";
import { ErrorNotice } from "../../components/ui/data";
import { Textarea } from "../../components/ui/form";
import { LanguageSettings } from "./LanguageSettings";
import { ModelSelection } from "./ModelSelection";
import { ProviderSettings } from "./ProviderSettings";
import { MinerUSettings } from "./MinerUSettings";
import { SettingsActionBar, SettingsGroup, SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";
import { renameRegistryId, type RegistryGroup } from "./registryEdits";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;
const noPendingCredentials = () => false;

export function GlobalConfiguration({ section }: { section: SettingsSectionId }) {
  const { t } = useI18n();
  const location = useLocation();
  const qc = useQueryClient();
  const credentials = platform().capabilities.credentials;
  // The platform is fixed before React mounts, so the hook identity is stable.
  const useCredentialsPending = credentials?.usePending ?? noPendingCredentials;
  const credentialsPending = useCredentialsPending();
  const Updates = platform().capabilities.updates?.Section;
  const Plugins = platform().capabilities.plugins?.Section;
  const Diagnostics = platform().capabilities.diagnostics?.Section;
  const mutations = useIsMutating();
  const [installing, setInstalling] = useState(false);
  const query = useQuery({
    queryKey: ["globalConfig"],
    queryFn: api.getGlobalConfig,
  });
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const [draft, setDraft] = useState<GlobalConfig | null>(null);
  const [yamlDirty, setYamlDirty] = useState(false);
  const [uncommitted, setUncommitted] = useState(false);
  const [registryKey, setRegistryKey] = useState(0);
  const [editingIds, setEditingIds] = useState(false);
  const [renames, setRenames] = useState<Record<string, string>>({});
  const [providerRenames, setProviderRenames] = useState<Record<string, string>>({});
  useEffect(() => {
    if (query.data && !draft) setDraft(query.data);
  }, [query.data, draft]);
  useEffect(() => () => credentials?.retain([]), [credentials]);
  const input = () => ({
    yaml: draft!.yaml,
    revision: draft!.revision,
    default_template: "标准翻译" as const,
    model_renames: renames,
    ...(credentials ? { provider_renames: providerRenames } : {}),
  });
  const apply = (value: GlobalConfig) => {
    setDraft(value);
    setYamlDirty(false);
  };
  const save = useMutation({
    mutationFn: async () => {
      // Keep submitted keys outside the mutation cache and independent of the page draft.
      const commitCredentials = credentials?.prepareCommit();
      const value = await api.saveGlobalConfig(input());
      // Keys are written only after their connections exist in the saved configuration.
      let keysFailed = false;
      if (commitCredentials) {
        try {
          await commitCredentials(Object.keys(object(object(value.effective.llm).providers)));
        } catch {
          keysFailed = true;
        }
      }
      return { value, keysFailed };
    },
    onSuccess: ({ value, keysFailed }) => {
      apply(value);
      setUncommitted(false);
      setRenames({});
      setProviderRenames({});
      if (credentials) void credentials.refresh();
      qc.setQueryData(["globalConfig"], value);
      for (const key of ["config", "models", "templates"])
        void qc.invalidateQueries({ queryKey: [key] });
      if (keysFailed) toast.error(t("settings.credentialsFailed"));
      else toast.success(t("settings.globalSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: () => api.validateGlobalConfig(input()),
    onSuccess: (value) => {
      setUncommitted((current) => current || draft?.yaml !== query.data?.yaml);
      apply(value);
      toast.success(t("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: api.getGlobalDefaults,
    onSuccess: (value) => {
      setUncommitted(true);
      apply({ ...value, revision: draft!.revision });
      setRenames({});
      setProviderRenames({});
      setRegistryKey((value) => value + 1);
      credentials?.retain(Object.keys(object(object(value.effective.llm).providers)));
      toast.success(t("settings.defaultsLoaded"));
    },
  });
  const error = save.error || validate.error || restore.error;
  const pending = save.isPending || validate.isPending || restore.isPending || installing;
  const disabled = !draft || yamlDirty || pending;
  const effective = draft?.effective || {};
  const llm = object(effective.llm);
  const configSaved = !!draft && draft.yaml === query.data?.yaml;
  const dirty = !configSaved || uncommitted || yamlDirty || credentialsPending;
  const change = (next: Document) => {
    if (draft)
      setDraft({
        ...draft,
        effective: next,
        yaml: JSON.stringify(next, null, 2),
      });
  };
  const changeLlm = (value: Document) => {
    change({ ...effective, llm: { ...value, preset: null } });
    credentials?.retain(Object.keys(object(value.providers)));
    setProviderRenames((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([, id]) => id in object(value.providers)),
      ),
    );
    setRenames((current) =>
      Object.fromEntries(
        Object.entries(current).filter(([, id]) => id in object(value.models)),
      ),
    );
  };
  const rename = (group: RegistryGroup, oldId: string, newId: string) => {
    if (group === "providers" && credentials) {
      credentials.rename(oldId, newId);
      const next = { ...providerRenames };
      const original = Object.keys(next).find((id) => next[id] === oldId);
      if (original) {
        if (original === newId) delete next[original];
        else next[original] = newId;
      } else if (oldId in object(object(query.data?.effective.llm).providers)) {
        next[oldId] = newId;
      }
      setProviderRenames(next);
    }
    if (group === "models") {
      const next = { ...renames };
      const original = Object.keys(next).find((id) => next[id] === oldId);
      if (original) {
        if (original === newId) delete next[original];
        else next[original] = newId;
      } else if (
        oldId in object(object(query.data?.effective.llm).models) &&
        !(oldId in next)
      )
        next[oldId] = newId;
      setRenames(next);
    }
    changeLlm(renameRegistryId(llm, group, oldId, newId));
  };
  return (
    <>
      <ErrorNotice error={query.error || error} />
      {/* Keep controls mounted so category navigation preserves local ID/key drafts. */}
      <div className="flex-1">
        <div hidden={section !== "general"}>
          <LanguageSettings />
        </div>
        <div hidden={section !== "providers"}>
          <SettingsSection id="provider-models" title={t("settings.sectionProviders")}>
            <ProviderSettings
              key={registryKey}
              config={effective}
              kinds={caps?.providers || []}
              disabled={disabled}
              onChange={changeLlm}
              onRename={rename}
              onEditingChange={setEditingIds}
              saved={configSaved && !credentialsPending}
            />
          </SettingsSection>
        </div>
        <div hidden={section !== "defaults"}>
          <SettingsSection title={t("settings.sectionDefaultModels")}>
            <ModelSelection
              scope="global"
              llm={llm}
              models={object(llm.models)}
              operations={caps?.operations}
              disabled={disabled}
              onChange={changeLlm}
            />
          </SettingsSection>
        </div>
        <div hidden={section !== "workflow"}>
          <SettingsSection title={t("settings.sectionWorkflow")}>
            <WorkflowSettings
              scope="global"
              config={effective}
              disabled={disabled}
              pdf
              error={error}
              onField={(group, key, value) => change(workflowField(effective, group, key, value))}
            />
          </SettingsSection>
        </div>
        <div hidden={section !== "mineru"}>
          <MinerUSettings />
        </div>
        {Plugins && (
          <div hidden={section !== "plugins"}>
            <LazyBoundary>
              <Plugins disabled={installing} />
            </LazyBoundary>
          </div>
        )}
        <div hidden={section !== "advanced"}>
          <SettingsSection id="advanced-yaml" title={t("settings.sectionAdvanced")}>
            <SettingsGroup
              title={t("settings.advancedYamlConfiguration")}
              summary={yamlDirty ? t("settings.unsavedSummary") : undefined}
            >
              <Textarea
                aria-label={t("settings.advancedYamlConfiguration")}
                spellCheck={false}
                className="min-h-[420px] font-mono text-xs"
                value={draft?.yaml || ""}
                disabled={!draft || pending}
                onChange={(event) => {
                  if (draft) {
                    setDraft({ ...draft, yaml: event.target.value });
                    setYamlDirty(true);
                  }
                }}
              />
            </SettingsGroup>
          </SettingsSection>
        </div>
        {Diagnostics && (
          <div hidden={section !== "diagnostics"}><Diagnostics /></div>
        )}
        {Updates && (
          <div hidden={section !== "updates"}>
            <LazyBoundary>
              <Updates
                blocked={!draft || !query.data || draft.yaml !== query.data.yaml ||
                  uncommitted || yamlDirty || editingIds || pending || mutations > 0 ||
                  Object.keys(renames).length > 0 || Object.keys(providerRenames).length > 0}
                onInstalling={setInstalling}
              />
            </LazyBoundary>
          </div>
        )}
      </div>
      <SettingsActionBar
        notices={
          <>
            {yamlDirty && (
              <Link to="/settings/advanced" state={location.state} className="block text-amber-700 underline underline-offset-4 dark:text-amber-300">
                {t("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
              </Link>
            )}
            {editingIds && (
              <Link to="/settings/providers" state={location.state} className="block text-amber-700 underline underline-offset-4 dark:text-amber-300">
                {t("registry.finishRenaming")}
              </Link>
            )}
            {!yamlDirty && !editingIds && dirty && (
              <span className="text-muted-foreground">{t("settings.unsavedSummary")}</span>
            )}
          </>
        }
      >
        <Button variant="ghost" disabled={!draft || pending} onClick={() => restore.mutate()}>
          {t("settings.restoreDefaults")}
        </Button>
        <Button
          variant="outline"
          disabled={!draft || pending || editingIds}
          onClick={() => validate.mutate()}
        >
          {t("settings.validateConfiguration")}
        </Button>
        <Button disabled={!draft || pending || editingIds} onClick={() => save.mutate()}>
          {pending ? t("common.saving") : t("settings.saveConfiguration")}
        </Button>
      </SettingsActionBar>
    </>
  );
}
