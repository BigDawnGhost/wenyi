import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { Link, useLocation, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "@/lib/toast";
import { api, isProjectBusy, type ProjectConfig } from "@/lib/api";
import { useProjectQuery } from "@/lib/projectQuery";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Disclosure } from "@/components/ui/disclosure";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/form";
import { ErrorNotice, ReadOnlyNotice, StructuredData } from "@/components/ui/data";

import { ModelSelection } from "./ModelSelection";
import { SettingsActionBar, SettingsSection } from "./SettingsSection";
import { WorkflowSettings, workflowField } from "./WorkflowSettings";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

export default function SettingsPage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const location = useLocation();
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  const [loaded, setLoaded] = useState(false);
  const [yamlDirty, setYamlDirty] = useState(false);
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const { data: project } = useProjectQuery(pid);
  const { data: caps } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const routes = useQuery({
    queryKey: ["models", pid],
    queryFn: () => api.modelRoutes(pid),
  });
  const apply = (value: ProjectConfig) => {
    setDraft(value.yaml);
    setEffective(value.effective);
    setYamlDirty(false);
    setLoaded(true);
  };
  useEffect(() => {
    if (config.data && !loaded) apply(config.data);
  }, [config.data, loaded]);
  useEffect(() => {
    setLoaded(false);
  }, [pid]);
  const save = useMutation({
    mutationFn: () => api.saveConfig(pid, draft),
    onSuccess: (value) => {
      apply(value);
      qc.invalidateQueries({ queryKey: ["config", pid] });
      qc.invalidateQueries({ queryKey: ["models", pid] });
      toast.success(tr("settings.projectSettingsSaved"));
    },
  });
  const validate = useMutation({
    mutationFn: () => api.validateConfig(pid, draft),
    onSuccess: (value) => {
      apply(value);
      toast.success(tr("settings.configurationIsValidButNotSavedYet"));
    },
  });
  const restore = useMutation({
    mutationFn: () => api.getProjectDefaults(pid),
    onSuccess: (value) => {
      apply(value);
      toast.success(tr("settings.defaultsLoaded"));
    },
  });
  const check = useMutation({
    mutationFn: () =>
      api.checkModels(pid, project?.fmt === "srt" ? "srt" : "translate"),
  });
  const busy =
    isProjectBusy(project?.status) ||
    (!project && config.data?.editable === false);
  const subtitles = project?.fmt === "srt";
  const setField = (group: string, key: string, value: unknown) => {
    const next = workflowField(effective, group, key, value);
    setEffective(next);
    setDraft(JSON.stringify(next, null, 2));
  };
  const configurationError = save.error || validate.error || restore.error;
  const formDisabled =
    busy ||
    yamlDirty ||
    !loaded ||
    save.isPending ||
    validate.isPending ||
    restore.isPending;
  return (
    <>
      <PageHeader title={tr("common.projectSettings")} />
      <PageContainer className="max-w-5xl">
        <ErrorNotice error={config.error || configurationError} />
        {busy && <ReadOnlyNotice className="mb-4" />}
        <SettingsSection title={tr("settings.sectionModels")}>
          <ModelSelection
            scope="project"
            llm={section(effective, "llm")}
            models={config.data?.registered_models || {}}
            operations={caps?.operations}
            disabled={formDisabled}
            onChange={(llm) => {
              const next = { ...effective, llm };
              setEffective(next);
              setDraft(JSON.stringify(next, null, 2));
            }}
          />
          <Link
            to="/settings/providers"
            state={{ settingsReturnTo: `${location.pathname}${location.search}${location.hash}` }}
            className="inline-block text-sm text-muted-foreground underline underline-offset-4"
          >
            {tr("settings.manageGlobalModels")}
          </Link>
        </SettingsSection>
        <SettingsSection title={tr("settings.sectionWorkflow")}>
          <WorkflowSettings
            scope="project"
            config={effective}
            disabled={formDisabled}
            error={configurationError}
            subtitles={subtitles}
            pdf={project?.fmt === "pdf"}
            onField={setField}
          />
        </SettingsSection>
        <SettingsSection id="advanced-yaml" title={tr("settings.sectionAdvanced")}>
          <Disclosure
            title={tr("settings.advancedYamlConfiguration")}
            summary={yamlDirty ? tr("settings.unsavedSummary") : undefined}
            error={configurationError}
          >
            <Textarea
              aria-label={tr("settings.advancedYamlConfiguration")}
              spellCheck={false}
              className="min-h-[420px] font-mono text-xs"
              value={draft}
              disabled={busy || !loaded}
              onChange={(e) => {
                setDraft(e.target.value);
                setYamlDirty(true);
              }}
            />
          </Disclosure>
          {validate.data && (
            <Disclosure title={tr("settings.validatedModelRoutes")}>
              <StructuredData value={validate.data.routes} />
            </Disclosure>
          )}
          <Disclosure
            title={tr("settings.savedModelRoutes")}
            error={routes.error || check.error}
          >
            <Button
              variant="outline"
              size="sm"
              disabled={check.isPending}
              onClick={() => check.mutate()}
            >
              {tr("settings.checkModelConfiguration")}
            </Button>
            <ErrorNotice error={routes.error || check.error} />
            <ModelRoutes value={routes.data} />
            {check.data !== undefined && (
              <div className="rounded border p-3">
                <StructuredData value={check.data} />
              </div>
            )}
          </Disclosure>
        </SettingsSection>
        <SettingsActionBar
          notices={
            yamlDirty && (
              <a
                href="#advanced-yaml"
                className="block text-amber-700 underline underline-offset-4 dark:text-amber-300"
              >
                {tr("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
              </a>
            )
          }
        >
          <Button
            variant="ghost"
            disabled={!loaded || busy || restore.isPending || save.isPending || validate.isPending}
            onClick={() => restore.mutate()}
          >
            {tr("settings.restoreDefaults")}
          </Button>
          <Button
            variant="outline"
            disabled={!loaded || busy || validate.isPending || restore.isPending}
            onClick={() => validate.mutate()}
          >
            {validate.isPending ? tr("settings.validating") : tr("settings.validateConfiguration")}
          </Button>
          <Button
            disabled={!loaded || busy || save.isPending || restore.isPending}
            onClick={() => save.mutate()}
          >
            {save.isPending ? tr("common.saving") : tr("settings.saveConfiguration")}
          </Button>
        </SettingsActionBar>
      </PageContainer>
    </>
  );
}

function ModelRoutes({ value }: { value: unknown }) {
  const { t: tr } = useI18n();
  if (!Array.isArray(value)) return <StructuredData value={value} />;
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="border-b text-xs text-muted-foreground">
          <tr>
            {[
              tr("common.actions"),
              tr("settings.modelProfileModel"),
              tr("common.provider"),
              tr("settings.tier"),
              tr("settings.fallbackModels"),
            ].map((label) => (
              <th key={label} className="text-left py-2 pr-4 font-medium">
                {label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {value.map((route: Record<string, unknown>, index) => (
            <tr
              key={String(route.operation || index)}
              className="border-b last:border-0"
            >
              <td className="py-3 pr-4 font-mono text-xs">
                {String(route.operation || "—")}
              </td>
              <td className="py-3 pr-4">
                <div>{String(route.profile || route.model || "—")}</div>
                <div className="text-xs text-muted-foreground">
                  {String(route.model || "")}
                </div>
              </td>
              <td className="py-3 pr-4">{String(route.provider || "—")}</td>
              <td className="py-3 pr-4">
                {String(route.tier || tr("settings.custom"))}
              </td>
              <td className="py-3 pr-4">
                {Array.isArray(route.fallbacks) && route.fallbacks.length
                  ? route.fallbacks.join(", ")
                  : "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <details className="mt-3">
        <summary className="cursor-pointer text-xs text-muted-foreground">
          {tr("settings.fullRouteParameters")}
        </summary>
        <pre className="mt-3 max-h-96 overflow-auto whitespace-pre-wrap break-all text-xs">
          {JSON.stringify(value, null, 2)}
        </pre>
      </details>
    </div>
  );
}
