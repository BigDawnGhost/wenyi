import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type ProjectConfig } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Card, CardContent } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input, Label, Select, Textarea } from "@/components/ui/form";
import { ErrorNotice, StructuredData } from "@/components/ui/data";

import { LanguageSettings } from "./LanguageSettings";
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
    ["annotation_alignment", tr("settings.paragraphAnnotationAlignment")],
  ];

  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [draft, setDraft] = useState("");
  const [effective, setEffective] = useState<Record<string, unknown>>({});
  const [loaded, setLoaded] = useState(false);
  const [yamlDirty, setYamlDirty] = useState(false);
  const [comparisonId, setComparisonId] = useState<string>();
  const [models, setModels] = useState("");
  const [operation, setOperation] = useState("translation.body");
  const [message, setMessage] = useState("");
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
  const comparison = useQuery({
    queryKey: ["comparison", pid, comparisonId],
    queryFn: () => api.getComparison(pid, comparisonId!),
    enabled: !!comparisonId,
    refetchInterval: (q) =>
      ["pending", "queued", "running"].includes(
        String(q.state.data?.status || "pending"),
      )
        ? 2000
        : false,
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
    setComparisonId(undefined);
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
  const compare = useMutation({
    mutationFn: () =>
      api.compareModels(pid, {
        operation,
        models: models
          .split(",")
          .map((s) => s.trim())
          .filter(Boolean),
        messages: [{ role: "user", content: message }],
      }),
    onSuccess: (job) => {
      setComparisonId(job.job_id);
      toast.success(tr("settings.modelComparisonStarted"));
    },
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
  const formDisabled =
    busy || yamlDirty || !loaded || save.isPending || validate.isPending;
  const activeComparison =
    comparisonId &&
    ["pending", "queued", "running"].includes(
      String(comparison.data?.status || "pending"),
    );

  return (
    <>
      <PageHeader
        title={tr("common.projectSettingsModels")}
        subtitle={tr("settings.configurationIsValidatedOnTheServerAdvanced")}
      />
      <PageContainer className="max-w-5xl space-y-4">
        <LanguageSettings />
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
                      section(effective, "segment").max_tokens_per_batch ||
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
                      section(effective, "segment").max_tokens_per_segment ||
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
                        section(effective, "pipeline").review_concurrency || 4,
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
                <div>
                  <Label htmlFor="pdf-backend">
                    {tr("settings.pdfParser")}
                  </Label>
                  <Select
                    id="pdf-backend"
                    value={String(
                      section(effective, "pipeline").pdf_backend || "mineru",
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
              </div>
              <label className="flex gap-2 items-center text-sm">
                <input
                  type="checkbox"
                  checked={Boolean(
                    section(effective, "output").punctuation_normalize,
                  )}
                  onChange={(e) =>
                    setField(
                      "output",
                      "punctuation_normalize",
                      e.target.checked,
                    )
                  }
                />
                {tr("settings.normalizePunctuationOnExport")}
              </label>
            </fieldset>
            <details>
              <summary className="cursor-pointer font-medium text-sm">
                {tr("settings.advancedYamlConfiguration")}
              </summary>
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
            </details>
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
        <Card>
          <CardContent className="p-5 space-y-4">
            <div className="flex gap-3 items-center justify-between">
              <h2 className="font-medium">{tr("settings.savedModelRoutes")}</h2>
              <Button
                variant="outline"
                disabled={check.isPending}
                onClick={() => check.mutate()}
              >
                {tr("settings.checkModelConfiguration")}
              </Button>
            </div>
            <ErrorNotice error={routes.error || check.error} />
            <ModelRoutes value={routes.data} />
            {check.data !== undefined && (
              <div className="rounded border p-3">
                <StructuredData value={check.data} />
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5 space-y-4">
            <h2 className="font-medium">{tr("common.modelComparison")}</h2>
            <p className="text-sm text-muted-foreground">
              {tr("settings.runningAModelComparisonSendsTestMessages")}
            </p>
            <div className="grid sm:grid-cols-2 gap-4">
              <div>
                <Label htmlFor="comparison-operation">
                  {tr("settings.operationName")}
                </Label>
                <Select
                  id="comparison-operation"
                  value={operation}
                  onChange={(e) => setOperation(e.target.value)}
                  className="mt-2"
                >
                  {(Array.isArray(caps?.operations) ? caps.operations : []).map(
                    (op: Record<string, unknown>) => (
                      <option key={String(op.id)} value={String(op.id)}>
                        {String(op.id)} —{" "}
                        {String(op.description || op.label || "")}
                      </option>
                    ),
                  )}
                </Select>
              </div>
              <div>
                <Label htmlFor="comparison-models">
                  {tr("settings.modelIdsCommaSeparated")}
                </Label>
                <Input
                  id="comparison-models"
                  placeholder={tr("settings.useTheModelIdsConfiguredAbove")}
                  value={models}
                  onChange={(e) => setModels(e.target.value)}
                  className="mt-2"
                />
              </div>
            </div>
            <Textarea
              aria-label={tr("settings.modelComparisonTestMessage")}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              placeholder={tr("settings.enterTheSameTestMessageForAll")}
            />
            <Button
              disabled={
                !message.trim() ||
                !models.trim() ||
                !operation.trim() ||
                compare.isPending ||
                !!activeComparison ||
                busy
              }
              onClick={() => compare.mutate()}
            >
              {activeComparison || compare.isPending
                ? tr("settings.comparisonRunning")
                : tr("settings.runModelComparison")}
            </Button>
            <ErrorNotice error={compare.error || comparison.error} />
            {comparison.data && <StructuredData value={comparison.data} />}
          </CardContent>
        </Card>
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
