import { useI18n } from "@/i18n";
import { languageName } from "@/i18n/labels";
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { api, isProjectBusy } from "@/lib/api";
import { SourcePreview } from "./SourcePreview";
import { SourceFilePicker } from "./SourceFilePicker";

export default function CreateProject() {
  const { t: tr, locale } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [searchParams, setSearchParams] = useSearchParams();
  const [name, setName] = useState("");
  const [source, setSource] = useState("auto");
  const [target, setTarget] = useState("zh");
  const [pid, setPid] = useState<string | null>(searchParams.get("project"));
  const [file, setFile] = useState<File | null>(null);
  const [prepare, setPrepare] = useState(false);
  const [pdfBackend, setPdfBackend] = useState<"" | "mineru" | "babeldoc">("");
  const { data: caps, error: capsError } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const { data: project, error: projectError } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid!),
    enabled: !!pid,
    refetchInterval: (query) =>
      isProjectBusy(query.state.data?.status) ? 1500 : false,
  });
  const busy = isProjectBusy(project?.status);
  const { data: preview } = useQuery({
    queryKey: ["preview", pid],
    queryFn: () => api.getPreview(pid!),
    enabled: !!pid && !!project?.fmt,
    retry: false,
    refetchInterval: (query) => (!query.state.data && busy ? 1500 : false),
  });
  useEffect(() => {
    // Fetch the final preview even if the last polling request preceded completion.
    if (pid && project?.fmt && !busy)
      void queryClient.invalidateQueries({ queryKey: ["preview", pid] });
  }, [pid, project?.fmt, busy, queryClient]);
  useEffect(() => {
    if (project && pid) {
      setName(project.name);
      setSource(project.source_lang || "auto");
      setTarget(project.target_lang || "zh");
    }
  }, [project, pid]);
  const extensions = (caps?.input_formats || []).flatMap((format) =>
    format === "markdown"
      ? ["md", "markdown"]
      : format === "html"
        ? ["html", "htm"]
        : format === "text" || format === "txt"
          ? ["txt", "text"]
          : [format],
  );
  const extension = file?.name.split(".").pop()?.toLowerCase();
  const subtitle = extension === "srt";
  const fileError =
    file &&
    (file.size === 0
      ? tr("createProject.emptyFile")
      : caps && !extensions.includes(extension || "")
        ? tr("createProject.unsupportedFile")
        : null);
  const create = useMutation({
    mutationFn: () => {
      if (!file || fileError)
        throw new Error(fileError || tr("createProject.sourceRequired"));
      return api.createProject(
        {
          name: name.trim(),
          source_lang: source,
          target_lang: target,
          prepare: !subtitle && prepare,
          pdf_backend: extension === "pdf" && pdfBackend ? pdfBackend : null,
        },
        file,
      );
    },
    onSuccess: (p) => {
      queryClient.setQueryData(["project", p.id], p);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      setPid(p.id);
      setSearchParams({ project: p.id }, { replace: true });
    },
  });
  const resume = useMutation({
    mutationFn: () => api.resume(pid!),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["project", pid] }),
  });
  const start = useMutation({
    mutationFn: () => api.translate(pid!),
    onSuccess: () => navigate(`/projects/${pid}`),
  });
  const sameLanguage = source !== "auto" && source === target;
  const locked = !!pid || create.isPending;
  const interrupted =
    project?.status === "error" || project?.status === "paused";
  const filename = file?.name || project?.source_meta?.original_filename;

  return (
    <>
      <PageHeader
        title={tr("common.createProject")}
        subtitle={tr("createProject.introduction")}
      />
      <PageContainer className="max-w-3xl space-y-4">
        <ErrorNotice
          error={
            capsError ||
            projectError ||
            create.error ||
            resume.error ||
            start.error ||
            fileError ||
            project?.error
          }
        />
        <Card>
          <CardContent className="p-5 space-y-4">
            <h2 className="font-medium">
              {tr("createProject.projectAndLanguages")}
            </h2>
            <div>
              <Label htmlFor="project-name">
                {tr("createProject.projectName")}
              </Label>
              <Input
                id="project-name"
                value={name}
                disabled={locked}
                onChange={(e) => setName(e.target.value)}
                className="mt-2"
                placeholder={tr(
                  "createProject.forExampleEnglishTranslationOfAShort",
                )}
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="source-language">
                  {tr("createProject.sourceLanguage")}
                </Label>
                <Select
                  id="source-language"
                  value={source}
                  disabled={locked}
                  onChange={(e) => setSource(e.target.value)}
                  className="mt-2"
                >
                  <option value="auto">
                    {tr("progress.detectAutomatically")}
                  </option>
                  {caps?.languages
                    .filter((l) => l.code !== "auto")
                    .map((l) => (
                      <option key={l.code} value={l.code}>
                        {languageName(l.code, l.name, locale)} ({l.code})
                      </option>
                    ))}
                </Select>
              </div>
              <div>
                <Label htmlFor="target-language">
                  {tr("createProject.targetLanguage")}
                </Label>
                <Select
                  id="target-language"
                  value={target}
                  disabled={locked || !caps}
                  onChange={(e) => setTarget(e.target.value)}
                  className="mt-2"
                >
                  {caps?.languages
                    .filter((l) => l.code !== "auto")
                    .map((l) => (
                      <option key={l.code} value={l.code}>
                        {languageName(l.code, l.name, locale)} ({l.code})
                      </option>
                    ))}
                </Select>
              </div>
            </div>
            {sameLanguage && (
              <ErrorNotice
                error={tr("createProject.theSourceAndTargetLanguagesAreThe")}
              />
            )}
            {pid && (
              <p className="text-sm text-muted-foreground">
                {tr(
                  "createProject.projectCreatedChangingLanguagesOrSourceContent",
                )}
                <Link
                  className="text-primary underline ml-2"
                  to={`/projects/${pid}/settings`}
                >
                  {tr("common.projectSettings")}
                </Link>
              </p>
            )}
          </CardContent>
        </Card>
        <Card>
          <CardContent className="p-5 space-y-4">
            <h2 className="font-medium">{tr("createProject.uploadStep")}</h2>
            <p className="text-sm text-muted-foreground">
              {tr("createProject.uploadHelp", {
                formats: caps?.input_formats.join(" / ") || "—",
              })}
            </p>
            <SourceFilePicker
              filename={typeof filename === "string" ? filename : undefined}
              extensions={extensions}
              disabled={locked}
              onSelectFile={(file) => {
                setFile(file);
                create.reset();
              }}
            />
            {!pid && extension === "pdf" && (
              <div>
                <Label htmlFor="pdf-backend">{tr("settings.pdfParser")}</Label>
                <Select
                  id="pdf-backend"
                  className="mt-2"
                  value={pdfBackend}
                  disabled={locked}
                  onChange={(e) =>
                    setPdfBackend(e.target.value as typeof pdfBackend)
                  }
                >
                  <option value="">{tr("createProject.serverDefault")}</option>
                  <option value="mineru">MinerU</option>
                  <option value="babeldoc">BabelDOC</option>
                </Select>
              </div>
            )}
            {!pid && !subtitle && (
              <div className="flex items-start gap-3 rounded-lg border p-4">
                <input
                  id="prepare-source"
                  type="checkbox"
                  checked={prepare}
                  disabled={locked}
                  onChange={(e) => setPrepare(e.target.checked)}
                  aria-describedby="prepare-help"
                  className="mt-1 accent-primary"
                />
                <div>
                  <Label htmlFor="prepare-source">
                    {tr("createProject.prepareSource")}
                  </Label>
                  <p
                    id="prepare-help"
                    className="mt-1 text-sm text-muted-foreground"
                  >
                    {tr("createProject.prepareHelp")}
                  </p>
                </div>
              </div>
            )}
            {!pid && (
              <div className="space-y-2">
                <Button
                  onClick={() => create.mutate()}
                  disabled={
                    !name.trim() ||
                    !file ||
                    !!fileError ||
                    sameLanguage ||
                    !caps ||
                    create.isPending
                  }
                >
                  {create.isPending
                    ? tr("createProject.uploading")
                    : tr("common.createProject")}
                </Button>
                {!file && (
                  <p className="text-xs text-muted-foreground">
                    {tr("createProject.sourceRequired")}
                  </p>
                )}
              </div>
            )}
            {busy && (
              <p role="status" className="text-sm">
                {project?.status === "preparing"
                  ? tr("createProject.preparingSource")
                  : tr("createProject.parsingTheSourceAPreviewWillAppear")}
              </p>
            )}
            {preview && <SourcePreview preview={preview} />}
            {pid && (
              <div className="flex flex-wrap gap-3">
                {interrupted ? (
                  <Button
                    onClick={() => resume.mutate()}
                    disabled={resume.isPending}
                  >
                    {tr("progress.resumeTask")}
                  </Button>
                ) : (
                  <Button
                    onClick={() => start.mutate()}
                    disabled={!preview || busy || start.isPending}
                  >
                    {start.isPending
                      ? tr("createProject.starting")
                      : tr("common.startTranslation")}
                  </Button>
                )}
                <Link to={`/projects/${pid}`}>
                  <Button variant="outline">
                    {tr("createProject.openProject")}
                  </Button>
                </Link>
              </div>
            )}
          </CardContent>
        </Card>
      </PageContainer>
    </>
  );
}
