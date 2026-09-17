import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, type ExportFormat, type PdfEngine } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { StatusBadge } from "@/components/StatusBadge";
import { Disclosure } from "@/components/ui/disclosure";
import { Select, Label } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { cn, formatBytes } from "@/lib/utils";

export default function ExportPage() {
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
  const [pdfBackend, setPdfBackend] = useState<PdfEngine | "">("");
  const { data: project, error: projectError } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
  });
  const { data: caps, error: capsError } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
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
  const subtitle = project?.fmt === "srt";
  const formats: ExportFormat[] = subtitle
    ? ["srt"]
    : ((caps?.output_formats || []).filter(
        (f) => f !== "srt",
      ) as ExportFormat[]);
  const fmt = subtitle ? "srt" : format;
  const create = useMutation({
    mutationFn: () =>
      api.createExport(pid, {
        format: fmt || undefined,
        bilingual,
        order,
        about_page: subtitle ? false : about,
        preserve_source_style: preserveStyle,
        punctuation_normalize: punctuation,
        ...(pdfBackend && fmt === "pdf" ? { pdf_engine: pdfBackend } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["exports", pid] });
      toast.success(tr("export.exportTaskSubmitted"));
    },
  });
  const download = useMutation({
    mutationFn: (id: number) => api.downloadExport(pid, id),
  });

  return (
    <>
      <PageHeader
        title={tr("export.exportTranslation")}
        subtitle={tr(
          "export.generateFilesFromSavedTranslationsIncludingSnapshots",
        )}
      />
      <PageContainer className="space-y-4 max-w-4xl">
        <ErrorNotice
          error={
            projectError ||
            configError ||
            capsError ||
            exportsError ||
            create.error ||
            download.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <div>
              <Label>{tr("export.outputFormat")}</Label>
              <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-2">
                {!subtitle && (
                  <button
                    onClick={() => setFormat("")}
                    aria-pressed={!fmt}
                    className={cn(
                      "rounded border p-3 text-left text-sm",
                      !fmt && "border-primary ring-1 ring-primary",
                    )}
                  >
                    <strong>{tr("export.automatic")}</strong>
                    <p className="text-xs text-muted-foreground mt-1">
                      {tr("export.basedOnTheSourceFormatAndPdf")}
                    </p>
                  </button>
                )}
                {formats.map((f) => (
                  <button
                    key={f}
                    onClick={() => setFormat(f)}
                    aria-pressed={fmt === f}
                    className={cn(
                      "rounded border p-3 text-left text-sm",
                      fmt === f && "border-primary ring-1 ring-primary",
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
              <label className="text-sm flex gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={!bilingual}
                  onChange={() => setBilingual(false)}
                />
                {tr("export.monolingual")}
              </label>
              <label className="text-sm flex gap-2">
                <input
                  type="radio"
                  name="edition"
                  checked={bilingual}
                  onChange={() => setBilingual(true)}
                />
                {tr("export.bilingual")}
              </label>
            </fieldset>
            {!subtitle && (
              <Disclosure
                title={tr("export.advanced")}
                error={create.error}
                summary={[
                  bilingual
                    ? tr(
                        order === "target_first"
                          ? "export.translationFirst"
                          : "export.sourceFirst",
                      )
                    : "",
                  tr("export.aboutSummary", {
                    value: tr(about ? "data.yes" : "data.no"),
                  }),
                  bilingual
                    ? tr("export.styleSummary", {
                        value: tr(preserveStyle ? "data.yes" : "data.no"),
                      })
                    : "",
                  tr("export.punctuationSummary", {
                    value: tr(punctuation ? "data.yes" : "data.no"),
                  }),
                  fmt === "pdf" ? pdfBackend || tr("export.automatic") : "",
                ]
                  .filter(Boolean)
                  .join(" · ")}
              >
                <label className="flex gap-2 items-center text-sm">
                  <input
                    type="checkbox"
                    checked={punctuation}
                    onChange={(e) => setPunctuation(e.target.checked)}
                  />
                  {tr("settings.normalizePunctuationOnExport")}
                </label>
                {fmt === "pdf" && (
                  <div>
                    <Label htmlFor="pdf-export-engine">
                      {tr("export.pdfExportEngine")}
                    </Label>
                    <Select
                      id="pdf-export-engine"
                      value={pdfBackend}
                      onChange={(e) =>
                        setPdfBackend(e.target.value as PdfEngine | "")
                      }
                      className="mt-2"
                    >
                      <option value="">{tr("export.automatic")}</option>
                      {caps?.pdf?.export_backends?.map((b) => (
                        <option key={b} value={b}>
                          {b}
                        </option>
                      ))}
                    </Select>
                  </div>
                )}
                {bilingual && !subtitle && (
                  <Select
                    aria-label={tr("export.bilingualOrder")}
                    value={order}
                    onChange={(e) =>
                      setOrder(
                        e.target.value as "target_first" | "source_first",
                      )
                    }
                    className="max-w-40"
                  >
                    <option value="target_first">
                      {tr("export.translationFirst")}
                    </option>
                    <option value="source_first">
                      {tr("export.sourceFirst")}
                    </option>
                  </Select>
                )}
                {!subtitle && (
                  <label className="flex gap-2 items-center text-sm">
                    <input
                      type="checkbox"
                      checked={about}
                      onChange={(e) => setAbout(e.target.checked)}
                    />
                    {tr("export.includeAnAboutThisTranslationPage")}
                  </label>
                )}
                {!subtitle && bilingual && (
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
            )}
            <Button
              onClick={() => create.mutate()}
              disabled={
                create.isPending ||
                !project?.fmt ||
                !caps ||
                !defaultsLoaded ||
                !!configError
              }
            >
              {create.isPending
                ? tr("common.submitting")
                : tr("export.generateExport")}
            </Button>
            <p className="text-xs text-muted-foreground">
              {tr("export.filenameHelp", {
                language: project?.target_lang || "—",
              })}
            </p>
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
                          {tr("export.download")}
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
