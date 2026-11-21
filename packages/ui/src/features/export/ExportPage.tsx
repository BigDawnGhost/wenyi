import { useI18n } from "../../i18n";
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { api } from "../../api/client";
import type { ExportFormat, ExportOptions } from "../../api/types";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { Card, CardContent } from "../../components/ui/card";
import { StatusBadge } from "../../components/StatusBadge";
import { Label } from "../../components/ui/form";
import { ErrorNotice } from "../../components/ui/data";
import { Progress } from "../../components/ui/progress";
import { cn, formatBytes } from "../../lib/utils";
import { useProjectQuery } from "../../lib/projectQuery";
import { platform } from "../../platform";

export default function ExportPage() {
  const { pid = "" } = useParams();
  return <ProjectExport key={pid} pid={pid} />;
}

function ProjectExport({ pid }: { pid: string }) {
  const saveExport = platform().capabilities.saveExport;
  const { t: tr, locale } = useI18n();
  const qc = useQueryClient();
  const { data: config, error: configError } = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid),
  });
  const [format, setFormat] = useState<ExportFormat | "">("");
  const [editionOverride, setEditionOverride] = useState<boolean>();
  const output = (config?.effective.output || {}) as Record<string, unknown>;
  const bilingual = editionOverride ?? (config ? Boolean(output.bilingual) : undefined);
  const { data: project, error: projectError } = useProjectQuery(pid);
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
  const formats = (project?.output_formats || []) as ExportFormat[];
  const fmt = formats.includes(format as ExportFormat) ? format : "";
  const pdf = fmt === "pdf" || (!fmt && project?.fmt === "pdf" && formats.includes("pdf"));
  const create = useMutation({
    mutationFn: async () => {
      const options: ExportOptions = {
        format: fmt || undefined,
        bilingual,
        order: output.bilingual_order === "source_first" ? "source_first" : "target_first",
        about_page: output.about_page !== false,
        preserve_source_style: !pdf && Boolean(output.bilingual_preserve_source_style),
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
      <PageContainer className="mx-auto w-full max-w-5xl space-y-4">
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
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 mt-2">
                <button
                  onClick={() => setFormat("")}
                  aria-pressed={!fmt}
                  className={cn(
                    "rounded-lg border p-3 text-left text-sm transition-[background-color,border-color,box-shadow] duration-motion-fast ease-standard focus-visible:outline-none",
                    !fmt
                      ? "border-primary/50 bg-accent"
                      : "border-border hover:border-muted-foreground/40 hover:bg-hover",
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
                      "rounded-lg border p-3 text-left text-sm transition-[background-color,border-color,box-shadow] duration-motion-fast ease-standard focus-visible:outline-none",
                      fmt === f
                        ? "border-primary/50 bg-accent"
                        : "border-border hover:border-muted-foreground/40 hover:bg-hover",
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
                  checked={bilingual === false}
                  onChange={() => setEditionOverride(false)}
                />
                {tr("export.monolingual")}
              </label>
              <label className="text-sm leading-5 flex items-center gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={bilingual === true}
                  onChange={() => setEditionOverride(true)}
                />
                {tr("export.bilingual")}
              </label>
            </fieldset>
            {bilingual && pdf && (
              <p className="text-sm text-muted-foreground">{tr("export.pdfBilingualLayout")}</p>
            )}
            {project?.fmt && !formats.length && (
              <p role="status" className="text-sm text-muted-foreground">
                {tr("error.sourceNotReady")}{" "}
                <Link to={`/projects/${pid}`} className="underline">
                  {tr("common.translationOverview")}
                </Link>
              </p>
            )}
            <Button
              onClick={() => create.mutate()}
              disabled={
                create.isPending ||
                !project?.fmt ||
                !formats.length ||
                !config ||
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
              <thead className="border-b border-subtle text-xs tracking-wide text-muted-foreground">
                <tr>
                  {[
                    tr("export.format"),
                    tr("common.created"),
                    tr("export.size"),
                    tr("common.status"),
                    tr("common.actions"),
                  ].map((h) => (
                    <th key={h} className="text-left px-4 py-3.5">
                      {h}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {exports?.slice(0, 5).map((e) => (
                  <tr key={e.id} className="border-b border-subtle last:border-0">
                    <td className="px-4 py-3.5">
                      {e.format.toUpperCase()}
                      <div className="text-xs text-muted-foreground mt-1">
                        {e.options?.bilingual
                          ? tr("export.bilingual")
                          : tr("export.monolingual")}
                      </div>
                    </td>
                    <td className="px-4 py-3.5">
                      {e.created_at
                        ? new Date(e.created_at).toLocaleString(locale)
                        : "—"}
                    </td>
                    <td className="px-4 py-3.5">{formatBytes(e.size)}</td>
                    <td className="px-4 py-3.5">
                      <StatusBadge status={e.status} />
                      {e.error && (
                        <p className="mt-1 text-sm text-destructive">
                          {e.error}
                        </p>
                      )}
                    </td>
                    <td className="px-4 py-3.5">
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
