import { useI18n } from "../../i18n";
import { Link, useLocation, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, isProjectBusy } from "../../api/client";
import { useProjectQuery } from "../../lib/projectQuery";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { ErrorNotice } from "../../components/ui/data";

import { ConfigurationSaveStatus, ConfigurationYaml, useConfigurationEditor } from "./ConfigurationEditor";
import { ModelSelection } from "./ModelSelection";
import { ExportSettings } from "./ExportSettings";
import { SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";
import { SETTINGS_CONTENT_CLASSNAME } from "./SettingsRow";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

export default function SettingsPage() {
  const { pid = "" } = useParams();
  return <ProjectSettings key={pid} pid={pid} />;
}

function ProjectSettings({ pid }: { pid: string }) {
  const { t: tr } = useI18n();
  const location = useLocation();
  const qc = useQueryClient();
  const queryKey = ["config", pid];
  const config = useQuery({
    queryKey,
    queryFn: () => api.getConfig(pid),
  });
  const { data: project } = useProjectQuery(pid);
  const editor = useConfigurationEditor({
    scope: `project:${pid}`,
    saved: config.data,
    write: (yaml) => api.saveConfig(pid, yaml),
    onSaved: (value) => {
      void qc.cancelQueries({ queryKey, exact: true });
      qc.setQueryData(queryKey, value);
    },
    successMessage: "settings.projectSettingsSaved",
  });
  const busy =
    isProjectBusy(project?.status) ||
    (!project && config.data?.editable === false);
  const readOnly = busy || !config.data;
  const formDisabled = readOnly || editor.formBlocked;
  const { effective, change } = editor;
  const setField = (group: string, key: string, value: unknown) =>
    change(workflowField(effective, group, key, value));
  return (
    <div className="flex min-h-dvh flex-col">
      <PageHeader title={tr("common.projectSettings")} />
      <PageContainer className="mx-auto flex w-full max-w-5xl flex-1 flex-col">
        <div className={SETTINGS_CONTENT_CLASSNAME}>
          <ErrorNotice error={config.error} />
          <ConfigurationSaveStatus editor={editor} disabled={readOnly} />
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
              onChange={(llm) => change({ ...effective, llm })}
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
          <SettingsSection title={tr("settings.advancedYamlConfiguration")} className="py-0">
            <ConfigurationYaml editor={editor} disabled={readOnly} />
          </SettingsSection>
        </div>
      </PageContainer>
    </div>
  );
}
