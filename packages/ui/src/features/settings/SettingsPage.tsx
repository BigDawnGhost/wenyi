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
import { ErrorNotice } from "../../components/ui/data";

import { ModelSelection } from "./ModelSelection";
import { ExportSettings } from "./ExportSettings";
import { SettingsActionBar, SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";
import { SETTINGS_CONTENT_CLASSNAME } from "./SettingsRow";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

/**
 * Whether two configurations are the same document. Save and validate answer with
 * a freshly built object every time, so identity says nothing; comparing the
 * serialized form is enough here and avoids walking two trees.
 */
const sameConfig = (a: Record<string, unknown>, b: Record<string, unknown>) =>
  a === b || JSON.stringify(a) === JSON.stringify(b);

type ConfigRequest = { pid: string; yaml: string; revision: number };

export default function SettingsPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const location = useLocation();
  const qc = useQueryClient();
  // `effective` is the single source of truth for every field. The YAML editor
  // is a second view of the same document, not the state: keeping the serialized
  // string as the store meant every toggle had to re-stringify the whole config
  // and hand the form a brand-new object, so one switch re-rendered the page.
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  // Only the text the user typed into the YAML editor, or undefined when the
  // editor is simply showing the serialized form. Everything else reads through
  // yaml below.
  const [yamlText, setYamlText] = useState<string | undefined>(undefined);
  const [loadedPid, setLoadedPid] = useState<string | null>(null);
  const loaded = loadedPid === pid;
  const yamlDirty = yamlText !== undefined;
  const yaml = yamlText ?? JSON.stringify(effective, null, 2);
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const { data: project } = useProjectQuery(pid);
  const revision = useRef(0);
  const pidRef = useRef(pid);
  pidRef.current = pid;
  const apply = (value: ProjectConfig, forPid: string) => {
    // Responses from a save/validate started under the previous project must never
    // overwrite this project's draft and re-enable the form (audit Y-2/F-1).
    if (pidRef.current !== forPid) return;
    // Skip the write when the server echoed what is already on screen. It hands
    // back a fresh object every time, so setting it unconditionally replaced the
    // form state and re-rendered every control for no change at all.
    setEffective((current) => (sameConfig(current, value.effective) ? current : value.effective));
    setYamlText(undefined);
    setLoadedPid(forPid);
  };
  useEffect(() => {
    if (config.data && !loaded) apply(config.data, pid);
  }, [config.data, loaded, pid]);
  useEffect(() => {
    validate.reset();
  }, [pid]);
  const snapshot = (): ConfigRequest => ({ pid, yaml, revision: revision.current });
  const applyResponse = (value: ProjectConfig, request: ConfigRequest) => {
    // The form stays editable during requests. Do not erase newer local edits.
    if (request.revision === revision.current) apply(value, request.pid);
  };
  const save = useMutation({
    mutationFn: (request: ConfigRequest) => api.saveConfig(request.pid, request.yaml),
    onSuccess: (value, request) => {
      applyResponse(value, request);
      qc.invalidateQueries({ queryKey: ["config", request.pid] });
      toast.success(tr("settings.projectSettingsSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: (request: ConfigRequest) => api.validateConfig(request.pid, request.yaml),
    onSuccess: (value, request) => {
      applyResponse(value, request);
      toast.success(tr("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: (request: ConfigRequest) => api.getProjectDefaults(request.pid),
    onSuccess: (value, request) => {
      applyResponse(value, request);
      toast.success(tr("settings.defaultsLoaded"));
    },
  });
  const busy =
    isProjectBusy(project?.status) ||
    (!project && config.data?.editable === false);
  const setField = (group: string, key: string, value: unknown) => {
    revision.current++;
    setEffective((current) => workflowField(current, group, key, value));
    // Editing a field invalidates whatever the YAML editor was showing.
    setYamlText(undefined);
  };
  const configurationError = save.error || validate.error || restore.error;
  // Two different questions, kept apart. Whether the document can be edited at all
  // (busy project, not loaded yet, unvalidated YAML) is a property of the form.
  // Whether a request is in flight is a property of the buttons: disabling the
  // whole form for the length of a save or a restore applied the disabled style to
  // every control at once, and the page blinked.
  const readOnly = busy || !loaded;
  const formDisabled = readOnly || yamlDirty;
  const actionsBusy = save.isPending || validate.isPending || restore.isPending;
  // A ref, because state only catches up on the next render: clicking twice within
  // one frame would otherwise read the same stale value twice and fire both.
  const inFlight = useRef(false);
  inFlight.current = actionsBusy;
  const guard = (run: () => void) => {
    if (inFlight.current) return;
    inFlight.current = true;
    run();
  };
  return (
    <div className="flex min-h-dvh flex-col">
      <PageHeader title={tr("common.projectSettings")} />
      <PageContainer className="mx-auto flex w-full max-w-5xl flex-1 flex-col pb-24">
        <div className={SETTINGS_CONTENT_CLASSNAME}>
          <ErrorNotice error={config.error || configurationError} />
          <SettingsSection title={tr("settings.stageModels")} className="py-0" grouped>
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
                revision.current++;
                setEffective((current) => ({ ...current, llm }));
                setYamlText(undefined);
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
              value={yaml}
              disabled={readOnly}
              onChange={(e) => {
                revision.current++;
                setYamlText(e.target.value);
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
          disabled={readOnly}
          aria-busy={restore.isPending || undefined}
          onClick={() => guard(() => restore.mutate(snapshot()))}
        >
          {tr("settings.restoreDefaults")}
        </Button>
        {/* One label each. The outcome already arrives as a notification, so
            rewording the button while the request runs only made the bar, which
            centres on its own content, shift under the cursor. */}
        <Button
          variant="outline"
          disabled={readOnly}
          aria-busy={validate.isPending || undefined}
          onClick={() => guard(() => validate.mutate(snapshot()))}
        >
          {tr("settings.validateConfiguration")}
        </Button>
        <Button
          disabled={readOnly}
          aria-busy={save.isPending || undefined}
          onClick={() => guard(() => save.mutate(snapshot()))}
        >
          {tr("settings.saveConfiguration")}
        </Button>
      </SettingsActionBar>
    </div>
  );
}
