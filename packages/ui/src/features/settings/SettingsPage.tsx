import { useI18n } from "../../i18n";
import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { api, isProjectBusy } from "../../api/client";
import type { ProjectConfig } from "../../api/types";
import { useProjectQuery } from "../../lib/projectQuery";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { Textarea } from "../../components/ui/form";
import { ErrorNotice, ReadOnlyNotice } from "../../components/ui/data";

import { ModelSelection } from "./ModelSelection";
import { ExportSettings } from "./ExportSettings";
import { SettingsActionBar, SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";
import { SETTINGS_CONTENT_CLASSNAME } from "./SettingsRow";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

export default function SettingsPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const location = useLocation();
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  const [loadedPid, setLoadedPid] = useState<string | null>(null);
  const loaded = loadedPid === pid;
  const [yamlDirty, setYamlDirty] = useState(false);
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const { data: project } = useProjectQuery(pid);
  const apply = (value: ProjectConfig, forPid: string) => {
    // Responses from a save/validate started under the previous project must never
    // overwrite this project's draft and re-enable the form (audit Y-2/F-1).
    if (pidRef.current !== forPid) return;
    setDraft(value.yaml);
    setEffective(value.effective);
    setYamlDirty(false);
    setLoadedPid(forPid);
  };
  useEffect(() => {
    if (config.data && !loaded) apply(config.data, pid);
  }, [config.data, loaded, pid]);
  const pidRef = useRef(pid);
  pidRef.current = pid;
  useEffect(() => {
    validate.reset();
  }, [pid]);
  const save = useMutation({
    mutationFn: () => api.saveConfig(pid, draft),
    onSuccess: (value) => {
      apply(value, pid);
      qc.invalidateQueries({ queryKey: ["config", pid] });
      toast.success(tr("settings.projectSettingsSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: () => api.validateConfig(pid, draft),
    onSuccess: (value) => {
      apply(value, pid);
      toast.success(tr("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: () => api.getProjectDefaults(pid),
    onSuccess: (value) => {
      apply(value, pid);
      toast.success(tr("settings.defaultsLoaded"));
    },
  });
  const busy =
    isProjectBusy(project?.status) ||
    (!project && config.data?.editable === false);
  const setField = (group: string, key: string, value: unknown) => {
    const next = workflowField(effective, group, key, value);
    setEffective(next);
    setDraft(JSON.stringify(next, null, 2));
  };
  const configurationError = save.error || validate.error || restore.error;
  const pending = save.isPending || validate.isPending || restore.isPending;
  const disabled = busy || !loaded || pending;
  const formDisabled = disabled || yamlDirty;
  return (
    <div className="flex min-h-dvh flex-col">
      <PageHeader title={tr("common.projectSettings")} />
      <PageContainer className="mx-auto flex w-full max-w-5xl flex-1 flex-col pb-24">
        <div className={SETTINGS_CONTENT_CLASSNAME}>
          <ErrorNotice error={config.error || configurationError} />
          {busy && <ReadOnlyNotice />}
          <SettingsSection title={tr("settings.sectionDefaultModels")} className="py-0" grouped>
            <ModelSelection
              llm={section(effective, "llm")}
              models={config.data?.registered_models || {}}
              disabled={formDisabled}
              footer={
                <Link
                  to="/settings/providers"
                  state={{ settingsReturnTo: `${location.pathname}${location.search}${location.hash}` }}
                  className="inline-block text-sm text-muted-foreground underline underline-offset-4"
                >
                  {tr("settings.manageGlobalModels")}
                </Link>
              }
              onChange={(llm) => {
                const next = { ...effective, llm };
                setEffective(next);
                setDraft(JSON.stringify(next, null, 2));
              }}
            />
          </SettingsSection>
          <SettingsSection title={tr("settings.sectionTranslationStyle")} className="py-0" grouped>
            <WorkflowSettings
              scope="project"
              config={effective}
              disabled={formDisabled}
              onField={setField}
              part="translation-style"
            />
          </SettingsSection>
          <SettingsSection title={tr("settings.sectionWorkflow")} className="py-0" grouped>
            <WorkflowSettings
              scope="project"
              config={effective}
              disabled={formDisabled}
              onField={setField}
              part="pipeline"
            />
          </SettingsSection>
          <SettingsSection title={tr("settings.sectionExport")} className="py-0" grouped>
            <ExportSettings
              config={effective}
              disabled={formDisabled}
              onField={setField}
            />
          </SettingsSection>
          <SettingsSection id="advanced-yaml" title={tr("settings.advancedYamlConfiguration")} className="py-0">
            {yamlDirty && (
              <p className="text-sm text-muted-foreground">{tr("settings.unsavedSummary")}</p>
            )}
            <Textarea
              aria-label={tr("settings.advancedYamlConfiguration")}
              spellCheck={false}
              className="min-h-editor font-mono text-xs"
              value={draft}
              disabled={disabled}
              onChange={(e) => {
                setDraft(e.target.value);
                setYamlDirty(true);
              }}
            />
          </SettingsSection>
        </div>
      </PageContainer>
      {/* Outside PageContainer on purpose: that container runs a transform
          animation on entry, and a transformed ancestor becomes the
          containing block of fixed descendants. Nested inside, the bar was
          positioned against the animating page column for the length of the
          animation, which read as a sideways jump. */}
      <SettingsActionBar
        notices={
          yamlDirty && (
            <a
              href="#advanced-yaml"
              className="block text-warning-text underline underline-offset-4"
            >
              {tr("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
            </a>
          )
        }
      >
        <Button
          variant="outline"
          disabled={disabled}
          onClick={() => restore.mutate()}
        >
          {tr("settings.restoreDefaults")}
        </Button>
        <Button
          variant="outline"
          disabled={disabled}
          onClick={() => validate.mutate()}
        >
          {validate.isPending ? tr("settings.validating") : tr("settings.validateConfiguration")}
        </Button>
        <Button
          disabled={disabled}
          onClick={() => save.mutate()}
        >
          {save.isPending ? tr("common.saving") : tr("settings.saveConfiguration")}
        </Button>
      </SettingsActionBar>
    </div>
  );
}
