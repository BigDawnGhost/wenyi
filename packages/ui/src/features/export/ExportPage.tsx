import { useI18n } from "../../i18n";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { api } from "../../api/client";
import type { ExportFormat } from "../../api/types";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";
import { StatusBadge } from "../../components/StatusBadge";
import { Disclosure } from "../../components/ui/disclosure";
import { Label } from "../../components/ui/form";
import { Select, SelectItem } from "../../components/ui/select";
import { ErrorNotice } from "../../components/ui/data";
import { Progress } from "../../components/ui/progress";
import { cn, formatBytes } from "../../lib/utils";
import { platform } from "../../platform";

export default function ExportPage() {
  const saveExport = platform().capabilities.saveExport;
  const { t: tr, locale } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [defaultsLoaded, setDefaultsLoaded] = useState(false);
  const { data: config, error: configError } = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const [format, setFormat] = useState<ExportFormat | "">("");
  const [bilingual, setBilingual] = useState(false);
  const [order, setOrder] = useState<"target_first" | "source_first">(
    "target_first",
  );
  const [about, setAbout] = useState(true);
  const [punctuation, setPunctuation] = useState(true);
  const [preserveStyle, setPreserveStyle] = useState(false);
  const { data: project, error: projectError } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
  });
  const { data: exports, error: exportsError } = useQuery({
    queryKey: ["exports", pid],
    queryFn: () => api.listExports(pid),
    refetchInterval: (q) =>
      q.state.data?.some((e) =>
        ["pending", "running", "queued"].includes(e.status),
      )
        ? 2000
        : false,
  });
  useEffect(() => {
    if (config && !defaultsLoaded) {
      const output = (config.effective.output || {}) as Record<string, unknown>;
      setBilingual(Boolean(output.bilingual));
      setPunctuation(output.punctuation_normalize !== false);
      setAbout(output.about_page !== false);
      setPreserveStyle(Boolean(output.bilingual_preserve_source_style));
      setOrder(
        output.bilingual_order === "source_first"
          ? "source_first"
          : "target_first",
      );
      setDefaultsLoaded(true);
    }
  }, [config, defaultsLoaded]);
  const formats = (project?.output_formats || []) as ExportFormat[];
  const fmt = formats.includes(format as ExportFormat) ? format : "";
  const pdf = fmt === "pdf" || (!fmt && project?.fmt === "pdf" && formats.includes("pdf"));
  const create = useMutation({
    mutationFn: async () => {
      const options = {
        format: fmt || undefined,
        bilingual,
        order,
        about_page: about,
        preserve_source_style: !pdf && preserveStyle,
        punctuation_normalize: punctuation,
      };
      if (saveExport) {
        const saved = await saveExport(pid, options);
        if (saved) toast.success(tr("export.savedTo", { path: saved.path }));
      } else {
        await api.createExport(pid, options);
        toast.success(tr("export.exportTaskSubmitted"));
      }
      qc.invalidateQueries({ queryKey: ["exports", pid] });
    },
  });
  const download = useMutation({
    mutationFn: async (id: number) => {
      if (saveExport) {
        const saved = await saveExport(pid, {}, id);
        if (saved) toast.success(tr("export.savedTo", { path: saved.path }));
      } else {
        await api.downloadExport(pid, id);
      }
    },
  });

  return (
    <>
      <PageHeader title={tr("export.exportTranslation")} />
      <PageContainer className="space-y-4 max-w-4xl">
        <ErrorNotice
          error={
            projectError ||
            configError ||
            exportsError ||
            create.error ||
            download.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <div>
              <Label>{tr("export.outputFormat")}</Label>
              <div className="grid grid-cols-4 gap-2 mt-2">
                <button
                  onClick={() => setFormat("")}
                  aria-pressed={!fmt}
                  className={cn(
                    "rounded border p-3 text-left text-sm",
                    !fmt && "border-muted-foreground bg-accent",
                  )}
                >
                  {tr("export.automatic")}
                </button>
                {formats.map((f) => (
                  <button
                    key={f}
                    onClick={() => setFormat(f)}
                    aria-pressed={fmt === f}
                    className={cn(
                      "rounded border p-3 text-left text-sm",
                      fmt === f && "border-muted-foreground bg-accent",
                    )}
                  >
                    {f.toUpperCase()}
                  </button>
                ))}
              </div>
            </div>
            <fieldset className="flex flex-wrap gap-5 items-center">
              <legend className="text-sm font-medium mb-2">
                {tr("export.edition")}
              </legend>
              <label className="text-sm leading-5 flex items-center gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={!bilingual}
                  onChange={() => setBilingual(false)}
                />
                {tr("export.monolingual")}
              </label>
              <label className="text-sm leading-5 flex items-center gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={bilingual}
                  onChange={() => setBilingual(true)}
                />
                {tr("export.bilingual")}
              </label>
            </fieldset>
            {bilingual && pdf && (
              <p className="text-sm text-muted-foreground">{tr("export.pdfBilingualLayout")}</p>
            )}
            <Disclosure title={tr("export.advanced")} error={create.error}>
              <label className="flex gap-2 items-center text-sm">
                <input
                  type="checkbox"
                  checked={punctuation}
                  onChange={(e) => setPunctuation(e.target.checked)}
                />
                {tr("settings.normalizePunctuationOnExport")}
              </label>
              {bilingual && (
                <Select
                  aria-label={tr("export.bilingualOrder")}
                  value={order}
                  onValueChange={(value) =>
                    setOrder(
                      value as "target_first" | "source_first",
                    )
                  }
                  className="max-w-40"
                >
                  <SelectItem value="target_first">
                    {tr("export.translationFirst")}
                  </SelectItem>
                  <SelectItem value="source_first">
                    {tr("export.sourceFirst")}
                  </SelectItem>
                </Select>
              )}
              <label className="flex gap-2 items-center text-sm">
                <input
                  type="checkbox"
                  checked={about}
                  onChange={(e) => setAbout(e.target.checked)}
                />
                {tr("export.includeAnAboutThisTranslationPage")}
              </label>
              {bilingual && !pdf && (
                <label className="flex gap-2 items-center text-sm">
                  <input
                    type="checkbox"
                    checked={preserveStyle}
                    onChange={(e) => setPreserveStyle(e.target.checked)}
                  />
                  {tr("export.preserveSourceFormattingInBilingualOutput")}
                </label>
              )}
            </Disclosure>
            <Button
              onClick={() => create.mutate()}
              disabled={
                create.isPending ||
                !project?.fmt ||
                !formats.length ||
                !defaultsLoaded ||
                !!configError
              }
            >
              {create.isPending
                ? tr("common.submitting")
                : tr("export.generateExport")}
            </Button>
            {/*
              The native host reports no per-byte progress for a save, so this
              stays indeterminate rather than inventing a percentage. It is
              shown only while the promise is open, which is what tells a user
              a large book is still being written.
            */}
            {saveExport && create.isPending && (
              <div
                className="space-y-2"
                role="status"
                data-slot="native-export-progress"
              >
                <p className="text-sm font-medium">{tr("export.saving")}</p>
                <Progress
                  aria-label={tr("export.saving")}
                  indeterminate
                />
              </div>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-0 overflow-x-auto">
            <p className="px-3 py-4 text-sm text-muted-foreground">
              {tr("export.retentionHelp")}
            </p>
            <table className="w-full text-sm">
              <thead className="border-b text-xs text-muted-foreground">
                <tr>
                  {[
                    tr("export.format"),
                    tr("common.created"),
                    tr("export.size"),
                    tr("common.status"),
                    tr("common.actions"),
                  ].map((h) => (
                    <th key={h} className="text-left p-3">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {exports?.slice(0, 5).map((e) => (
                  <tr key={e.id} className="border-b last:border-0">
                    <td className="p-3">
                      {e.format.toUpperCase()}
                      <div className="text-xs text-muted-foreground mt-1">
                        {e.options?.bilingual
                          ? tr("export.bilingual")
                          : tr("export.monolingual")}
                      </div>
                    </td>
                    <td className="p-3">
                      {e.created_at
                        ? new Date(e.created_at).toLocaleString(locale)
                        : "—"}
                    </td>
                    <td className="p-3">{formatBytes(e.size)}</td>
                    <td className="p-3">
                      <StatusBadge status={e.status} />
                      {e.error && (
                        <p className="mt-1 text-sm text-destructive">
                          {e.error}
                        </p>
                      )}
                    </td>
                    <td className="p-3">
                      {e.status === "done" && (
                        <Button
                          variant="outline"
                          size="sm"
                          disabled={download.isPending}
                          onClick={() => download.mutate(e.id)}
                        >
                          {tr(saveExport ? "export.saveAs" : "export.download")}
                        </Button>
                      )}
                    </td>
                  </tr>
                ))}
                {!exports?.length && (
                  <tr>
                    <td
                      colSpan={5}
                      className="p-8 text-center text-muted-foreground"
                    >
                      {tr("export.noExportsYet")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
