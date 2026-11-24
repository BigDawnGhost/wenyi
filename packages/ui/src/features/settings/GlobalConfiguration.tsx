import { useEffect, useRef, useState } from "react";
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
import { cardSurface } from "../../components/ui/card";
import { cn } from "../../lib/utils";
import { ModelSelection } from "./ModelSelection";
import { ExportSettings } from "./ExportSettings";
import { ProviderSettings } from "./ProviderSettings";
import type { ProviderChanges } from "./ProviderDialog";
import { PdfSettings } from "./PdfSettings";
import { SettingsActionBar, SettingsGroup, SettingsSection } from "./SettingsSection";
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
  const [draft, setDraft] = useState<GlobalConfig | null>(null);
  const [yamlDirty, setYamlDirty] = useState(false);
  const [uncommitted, setUncommitted] = useState(false);
  const [providerSaving, setProviderSaving] = useState(false);
  useEffect(() => {
    if (query.data && !draft) setDraft(query.data);
  }, [query.data, draft]);
  const input = () => ({
    yaml: draft!.yaml,
    revision: draft!.revision,
    default_template: "标准翻译" as const,
  });
  const apply = (value: GlobalConfig) => {
    setDraft(value);
    setYamlDirty(false);
  };
  // Fields stay editable while a request runs. A reply answers the document that
  // was submitted, so if the draft has changed since, keep the newer edits and
  // take only the revision the server moved to.
  const latestDraft = useRef(draft);
  latestDraft.current = draft;
  const applyAnswer = (value: GlobalConfig, submittedYaml: string) => {
    if (latestDraft.current?.yaml === submittedYaml) apply(value);
    else setDraft((current) => current && { ...current, revision: value.revision });
  };
  const rememberSaved = (value: GlobalConfig) => {
    if (credentials) void credentials.refresh();
    qc.setQueryData(["globalConfig"], value);
    for (const key of ["config", "models", "templates"])
      void qc.invalidateQueries({ queryKey: [key] });
  };
  const save = useMutation({
    mutationFn: (body: ReturnType<typeof input>) => api.saveGlobalConfig(body),
    onSuccess: (value, body) => {
      applyAnswer(value, body.yaml);
      setUncommitted(false);
      rememberSaved(value);
      toast.success(t("settings.globalSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: (body: ReturnType<typeof input>) => api.validateGlobalConfig(body),
    onSuccess: (value, body) => {
      setUncommitted((current) => current || body.yaml !== query.data?.yaml);
      applyAnswer(value, body.yaml);
      toast.success(t("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: (_submittedYaml: string) => api.getGlobalDefaults(),
    onSuccess: (value, submittedYaml) => {
      if (latestDraft.current?.yaml !== submittedYaml) return;
      setUncommitted(true);
      apply({ ...value, revision: latestDraft.current.revision });
      toast.success(t("settings.defaultsLoaded"));
    },
  });
  const error = save.error || validate.error || restore.error;
  // Installing an app update is a state the page is in, so it disables the form
  // and the actions that would write configuration underneath the relaunch.
  // A request in flight is not: disabling for it applied the disabled face to
  // every control and stripped the hover state from the button being pressed.
  const pending = providerSaving || save.isPending || validate.isPending || restore.isPending;
  const disabled = !draft || yamlDirty || installing || providerSaving;
  const effective = draft?.effective || {};
  const llm = object(effective.llm);
  // Until the first load completes there is no draft to compare against, so
  // every derivation would read as "unsaved". Reporting that for one frame
  // both flashes the unsaved notice and, because the notice widens the action
  // bar, shifts the bar sideways before the load settles.
  const loaded = !!draft;
  const configSaved = loaded && draft.yaml === query.data?.yaml;
  const dirty = (loaded && !configSaved) || uncommitted || yamlDirty;
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
  };
  const savedLlm = object(query.data?.effective.llm);
  const registryEdited = ["providers", "models"].some(key => JSON.stringify(savedLlm[key]) !== JSON.stringify(llm[key]));
  const saveProvider = async ({ llm: next, models, provider, revision }: ProviderChanges) => {
    const saved = query.data!;
    setProviderSaving(true);
    try {
      // Save only the registry change; keep unrelated workflow and route edits in the draft.
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
      const savedRegistry = object(value.effective.llm);
      if (configSaved) {
        apply(value);
      } else {
        setDraft(current => {
          if (!current) return current;
          const currentLlm = object(current.effective.llm);
          const effective = {
            ...current.effective,
            llm: {
              ...currentLlm,
              preset: savedRegistry.preset,
              providers: savedRegistry.providers,
              models: savedRegistry.models,
              ...renameSelections(currentLlm, models),
            },
          };
          return { ...value, effective, yaml: JSON.stringify(effective, null, 2) };
        });
      }
      rememberSaved(value);
      return value;
    } finally {
      setProviderSaving(false);
    }
  };
  return (
    <>
      <ErrorNotice error={query.error || error} />
      <div className={SETTINGS_CONTENT_CLASSNAME}>
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
              draftLlm={llm}
              capabilities={caps}
              disabled={disabled || pending || registryEdited}
              onSave={saveProvider}
            />}
          </SettingsSection>
        </div>
        <div hidden={section !== "defaults"}>
          <SettingsSection title={t("settings.stageModels")} grouped>
            <ModelSelection
              llm={llm}
              models={object(llm.models)}
              registeredModels={draft?.registered_models}
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
            <SettingsGroup
              title={t("settings.advancedYamlConfiguration")}
              summary={yamlDirty ? t("settings.unsavedSummary") : undefined}
            >
              <div className={cn(cardSurface, "p-4")}>
                <Textarea
                  aria-label={t("settings.advancedYamlConfiguration")}
                  spellCheck={false}
                  className="min-h-editor font-mono text-xs"
                  value={draft?.yaml || ""}
                  disabled={!draft || pending || installing}
                  onChange={(event) => {
                    if (draft) {
                      setDraft({ ...draft, yaml: event.target.value });
                      setYamlDirty(true);
                    }
                  }}
                />
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
                blocked={!draft || !query.data || draft.yaml !== query.data.yaml ||
                  uncommitted || yamlDirty || pending || mutations > 0}
                onInstalling={setInstalling}
              />
            </LazyBoundary>
          </div>
        )}
      </div>
      <SettingsActionBar
        notices={
          yamlDirty || dirty ? (
            <>
              {yamlDirty && (
                <Link to="/settings/advanced" state={location.state} className="block text-warning-text underline underline-offset-4">
                  {t("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
                </Link>
              )}
              {!yamlDirty && dirty && (
                <span className="text-muted-foreground">{t("settings.unsavedSummary")}</span>
              )}
            </>
          ) : undefined
        }
      >
        <Button
          variant="outline"
          disabled={!draft || installing}
          aria-busy={restore.isPending || undefined}
          onClick={() => { if (!pending && !installing && draft) restore.mutate(draft.yaml); }}
        >
          {t("settings.restoreDefaults")}
        </Button>
        {section === "advanced" && (
          <Button
            variant="outline"
            disabled={!draft || installing}
            aria-busy={validate.isPending || undefined}
            onClick={() => { if (!pending && !installing && draft) validate.mutate(input()); }}
          >
            {t("settings.validateConfiguration")}
          </Button>
        )}
        <Button
          disabled={!draft || installing}
          aria-busy={save.isPending || undefined}
          onClick={() => { if (!pending && !installing && draft) save.mutate(input()); }}
        >
          {t("settings.saveConfiguration")}
        </Button>
      </SettingsActionBar>
    </>
  );
}
