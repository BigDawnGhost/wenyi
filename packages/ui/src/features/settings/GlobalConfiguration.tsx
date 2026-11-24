import { useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { useIsMutating, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../api/client";
import type { GlobalConfig } from "../../api/types";
import { platform } from "../../platform";
import { LazyBoundary } from "../../routes/LazyBoundary";
import type { SettingsSectionId } from "../../routes/manifest";
import { useI18n } from "../../i18n";
import { ErrorNotice } from "../../components/ui/data";
import { ConfigurationSaveStatus, ConfigurationYaml, useConfigurationEditor } from "./ConfigurationEditor";
import { LanguageSettings } from "./LanguageSettings";
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";
import { ModelSelection } from "./ModelSelection";
import { ExportSettings } from "./ExportSettings";
import { ProviderSettings } from "./ProviderSettings";
import type { ProviderChanges } from "./ProviderDialog";
import { PdfSettings } from "./PdfSettings";
import { SettingsGroup, SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";
import { renameSelections } from "./registryEdits";
import { SETTINGS_CONTENT_CLASSNAME } from "./SettingsRow";

type Document = Record<string, unknown>;
const object = (value: unknown) => (value || {}) as Document;

export function GlobalConfiguration({ section }: { section: SettingsSectionId }) {
  const { t } = useI18n();
  const location = useLocation();
  const qc = useQueryClient();
  const credentials = platform().capabilities.credentials;
  const Updates = platform().capabilities.updates?.Section;
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
  const [providerSaving, setProviderSaving] = useState(false);
  const rememberSaved = (value: GlobalConfig) => {
    void qc.cancelQueries({ queryKey: ["globalConfig"], exact: true });
    if (credentials) void credentials.refresh();
    qc.setQueryData(["globalConfig"], value);
    for (const key of ["config", "models", "templates"])
      void qc.invalidateQueries({ queryKey: [key] });
  };
  const editor = useConfigurationEditor({
    scope: "global",
    saved: query.data,
    write: (yaml, base) => api.saveGlobalConfig({
      yaml, revision: base.revision, default_template: base.default_template,
    }),
    onSaved: rememberSaved,
    successMessage: "settings.globalSaved",
  });
  const readOnly = !query.data || installing || providerSaving;
  const disabled = readOnly || editor.formBlocked;
  const { effective, change } = editor;
  const llm = object(effective.llm);
  const changeLlm = (value: Document) => {
    change({ ...effective, llm: { ...value, preset: null } });
  };
  const savedLlm = object(query.data?.effective.llm);
  const saveProvider = async ({ llm: next, models, provider, revision }: ProviderChanges) => {
    const saved = query.data!;
    setProviderSaving(true);
    try {
      const registry = { preset: next.preset, providers: next.providers, models: next.models };
      const value = await api.saveGlobalConfig({
        yaml: JSON.stringify({
          ...saved.effective,
          llm: { ...savedLlm, ...registry, ...renameSelections(savedLlm, models) },
        }),
        revision,
        default_template: saved.default_template,
        model_renames: models,
        ...(credentials && provider ? { provider_renames: { [provider.from]: provider.to } } : {}),
      });
      rememberSaved(value);
      return value;
    } finally {
      setProviderSaving(false);
    }
  };
  return (
    <>
      <ErrorNotice error={query.error} />
      <div className={SETTINGS_CONTENT_CLASSNAME}>
        <ConfigurationSaveStatus editor={editor} disabled={readOnly} />
        {editor.yamlDirty && section !== "advanced" && (
          <Link to="/settings/advanced" state={location.state} className="text-sm text-warning-text underline underline-offset-4">
            {t("settings.finishYamlChanges")}
          </Link>
        )}
        <div hidden={section !== "general"}>
          <LanguageSettings />
          <SettingsGroup title={t("settings.sectionExport")}>
            <ExportSettings
              config={effective}
              disabled={disabled}
              onField={(group, key, value) => change(workflowField(effective, group, key, value))}
            />
          </SettingsGroup>
        </div>
        <div hidden={section !== "providers"}>
          <SettingsSection id="provider-models" title={t("settings.sectionProviders")} grouped>
            {section === "providers" && <ProviderSettings
              revision={query.data?.revision ?? 0}
              llm={savedLlm}
              capabilities={caps}
              disabled={disabled || editor.pending || editor.dirty}
              onSave={saveProvider}
            />}
          </SettingsSection>
        </div>
        <div hidden={section !== "defaults"}>
          <SettingsSection title={t("settings.stageModels")} grouped>
            <ModelSelection
              llm={llm}
              models={object(llm.models)}
              registeredModels={query.data?.registered_models}
              disabled={disabled}
              onChange={changeLlm}
            />
          </SettingsSection>
        </div>
        <div hidden={section !== "workflow"}>
          <SettingsSection title={t("settings.sectionWorkflow")} grouped>
            <SettingsGroup title={t("settings.pipelineSteps")}>
              <WorkflowSettings
                scope="global"
                config={effective}
                disabled={disabled}
                onField={(group, key, value) => change(workflowField(effective, group, key, value))}
              />
            </SettingsGroup>
          </SettingsSection>
        </div>
        <div hidden={section !== "pdf"}>
          <PdfSettings disabled={installing} />
        </div>
        <div hidden={section !== "advanced"}>
          <SettingsSection id="advanced-yaml" title={t("settings.sectionAdvanced")} grouped>
            <SettingsGroup title={t("settings.advancedYamlConfiguration")}>
              <div className={cn(cardSurface, "p-4")}>
                <ConfigurationYaml editor={editor} disabled={readOnly} />
              </div>
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
                blocked={!query.data || editor.dirty || editor.pending || providerSaving || mutations > 0}
                onInstalling={setInstalling}
              />
            </LazyBoundary>
          </div>
        )}
      </div>
    </>
  );
}
