import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type ProjectConfig } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Disclosure } from "@/components/ui/disclosure";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select, Textarea } from "@/components/ui/form";
import { ErrorNotice, StructuredData } from "@/components/ui/data";

import { ProviderSettings } from "./ProviderSettings";

const section = (config: Record<string, unknown>, key: string) =>
  (config[key] || {}) as Record<string, unknown>;

export default function SettingsPage() {
  const { t: tr } = useI18n();
  const PIPELINE: [string, string][] = [
    ["book_understanding", tr("settings.bookUnderstanding")],
    ["polish", tr("settings.polishing")],
    ["review", tr("common.wholeBookReview")],
    ["review_autofix", tr("settings.applyAutofixesToTheSavedTranslation")],
  ];

  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  const [loaded, setLoaded] = useState(false);
  const [yamlDirty, setYamlDirty] = useState(false);
  const config = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
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
  const check = useMutation({
    mutationFn: () =>
      api.checkModels(pid, project?.fmt === "srt" ? "srt" : "translate"),
  });
  const busy =
    isProjectBusy(project?.status) ||
    (!project && config.data?.editable === false);
  const subtitles = project?.fmt === "srt";
  const setField = (group: string, key: string, value: unknown) => {
    const next = {
      ...effective,
      [group]: { ...section(effective, group), [key]: value },
    };
    setEffective(next);
    setDraft(JSON.stringify(next, null, 2));
  };
  const configurationError = save.error || validate.error;
  const formDisabled =
    busy || yamlDirty || !loaded || save.isPending || validate.isPending;
  return (
    <>
      <PageHeader
        title={tr("common.projectSettingsModels")}
        subtitle={tr("settings.configurationIsValidatedOnTheServerAdvanced")}
      />
      <PageContainer className="max-w-5xl space-y-4">
        <ErrorNotice error={config.error || save.error || validate.error} />
        {busy && (
          <p role="status" className="rounded border p-3 text-sm">
            {tr("settings.settingsAreReadOnlyWhileATask")}
          </p>
        )}
        <Card>
          <CardContent className="p-5 space-y-4">
            <ProviderSettings
              config={effective}
              disabled={formDisabled}
              error={configurationError}
              kinds={caps?.providers || []}
              onChange={(llm) => {
                const next = { ...effective, llm };
                setEffective(next);
                setDraft(JSON.stringify(next, null, 2));
              }}
            />
            <h2 className="font-medium">{tr("settings.workflowSettings")}</h2>
            {yamlDirty && (
              <p className="text-sm text-amber-700">
                {tr("settings.advancedYamlHasUnvalidatedChangesValidateIt")}
              </p>
            )}
            <fieldset
              disabled={formDisabled}
              className="space-y-4 disabled:opacity-60"
            >
              {!subtitles && (
                <div className="grid sm:grid-cols-2 gap-3">
                  {PIPELINE.map(([key, label]) => (
                    <label
                      key={key}
                      className="flex gap-2 items-center text-sm"
                    >
                      <input
                        type="checkbox"
                        checked={Boolean(section(effective, "pipeline")[key])}
                        onChange={(e) =>
                          setField("pipeline", key, e.target.checked)
                        }
                      />
                      {label}
                    </label>
                  ))}
                </div>
              )}
              {subtitles && (
                <p className="text-sm text-muted-foreground">
                  {tr("settings.subtitlesUseASeparateWorkflowWithoutBook")}
                </p>
              )}
              <Disclosure
                title={tr("settings.performance")}
                error={configurationError}
                summary={tr("settings.performanceSummary", {
                  tokens: Number(
                    section(effective, "segment").max_tokens_per_batch ?? 1800,
                  ),
                })}
              >
                {!subtitles && (
                  <label className="flex gap-2 items-center text-sm">
                    <input
                      type="checkbox"
                      checked={Boolean(
                        section(effective, "pipeline").annotation_alignment,
                      )}
                      onChange={(e) =>
                        setField(
                          "pipeline",
                          "annotation_alignment",
                          e.target.checked,
                        )
                      }
                    />
                    {tr("settings.paragraphAnnotationAlignment")}
                  </label>
                )}
                <div className="grid sm:grid-cols-2 gap-4">
                  <div>
                    <Label htmlFor="batch-tokens">
                      {tr("settings.tokensPerBatch")}
                    </Label>
                    <Input
                      id="batch-tokens"
                      type="number"
                      min={1}
                      value={Number(
                        section(effective, "segment").max_tokens_per_batch ??
                          1800,
                      )}
                      onChange={(e) =>
                        setField(
                          "segment",
                          "max_tokens_per_batch",
                          Number(e.target.value),
                        )
                      }
                      className="mt-2"
                    />
                  </div>
                  <div>
                    <Label htmlFor="segment-tokens">
                      {tr("settings.tokensPerParagraph")}
                    </Label>
                    <Input
                      id="segment-tokens"
                      type="number"
                      min={1}
                      value={Number(
                        section(effective, "segment").max_tokens_per_segment ??
                          1200,
                      )}
                      onChange={(e) =>
                        setField(
                          "segment",
                          "max_tokens_per_segment",
                          Number(e.target.value),
                        )
                      }
                      className="mt-2"
                    />
                  </div>
                  {!subtitles && (
                    <div>
                      <Label htmlFor="review-concurrency">
                        {tr("settings.reviewConcurrency")}
                      </Label>
                      <Input
                        id="review-concurrency"
                        type="number"
                        min={1}
                        value={Number(
                          section(effective, "pipeline").review_concurrency ??
                            4,
                        )}
                        onChange={(e) =>
                          setField(
                            "pipeline",
                            "review_concurrency",
                            Number(e.target.value),
                          )
                        }
                        className="mt-2"
                      />
                    </div>
                  )}
                  {project?.fmt === "pdf" && (
                    <div>
                      <Label htmlFor="pdf-backend">
                        {tr("settings.pdfParser")}
                      </Label>
                      <Select
                        id="pdf-backend"
                        value={String(
                          section(effective, "pipeline").pdf_backend ||
                            "mineru",
                        )}
                        onChange={(e) =>
                          setField("pipeline", "pdf_backend", e.target.value)
                        }
                        className="mt-2"
                      >
                        {(caps?.pdf?.backends || ["mineru", "babeldoc"]).map(
                          (s) => (
                            <option key={s} value={s}>
                              {s}
                            </option>
                          ),
                        )}
                      </Select>
                    </div>
                  )}
                </div>
              </Disclosure>
            </fieldset>
            <Disclosure
              title={tr("settings.advancedYamlConfiguration")}
              summary={tr(
                yamlDirty ? "settings.unsavedSummary" : "settings.yamlSummary",
              )}
              error={configurationError}
            >
              <p className="text-xs text-muted-foreground mt-3">
                {tr("settings.useServerEnvironmentVariableNamesForApi")}
              </p>
              <Textarea
                aria-label={tr("settings.advancedYamlConfiguration")}
                spellCheck={false}
                className="mt-3 min-h-[420px] font-mono text-xs"
                value={draft}
                disabled={busy || !loaded}
                onChange={(e) => {
                  setDraft(e.target.value);
                  setYamlDirty(true);
                }}
              />
            </Disclosure>
            <div className="flex gap-3">
              <Button
                variant="outline"
                disabled={!loaded || busy || validate.isPending}
                onClick={() => validate.mutate()}
              >
                {validate.isPending
                  ? tr("settings.validating")
                  : tr("settings.validateConfiguration")}
              </Button>
              <Button
                disabled={!loaded || busy || save.isPending}
                onClick={() => save.mutate()}
              >
                {save.isPending
                  ? tr("common.saving")
                  : tr("settings.saveConfiguration")}
              </Button>
            </div>
            {validate.data && (
              <details>
                <summary className="cursor-pointer text-sm">
                  {tr("settings.validatedModelRoutes")}
                </summary>
                <div className="mt-3">
                  <StructuredData value={validate.data.routes} />
                </div>
              </details>
            )}
          </CardContent>
        </Card>
        <Disclosure
          title={tr("settings.savedModelRoutes")}
          summary={tr("settings.routeSummary", {
            count: Array.isArray(routes.data) ? routes.data.length : 0,
          })}
          error={routes.error || check.error}
        >
          <Button
            variant="outline"
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
