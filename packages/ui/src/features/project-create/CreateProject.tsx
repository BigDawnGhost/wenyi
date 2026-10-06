import { useI18n } from "@/i18n";
import { languageName } from "@/i18n/labels";
import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate, useSearchParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/form";
import { Dialog } from "@/components/ui/misc";
import { Select, SelectItem } from "@/components/ui/select";
import { ErrorNotice } from "@/components/ui/data";
import { api, isProjectBusy } from "@/lib/api";
import { cn } from "@/lib/utils";
import { SourcePreview } from "./SourcePreview";
import { SourceFilePicker } from "./SourceFilePicker";
import { releaseSource, type ProjectSource } from "@/platform";

export default function CreateProjectDialog() {
  const { t: tr, locale } = useI18n();
  const navigate = useNavigate();
  const location = useLocation();
  const queryClient = useQueryClient();
  const [searchParams] = useSearchParams();
  const [name, setName] = useState("");
  const [source, setSource] = useState("auto");
  const [target, setTarget] = useState("zh");
  const [pid] = useState<string | null>(searchParams.get("project"));
  const [file, setFile] = useState<ProjectSource | null>(null);
  const [sourceConsumed, setSourceConsumed] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);
  const mounted = useRef(false);
  const uploadingSource = useRef<ProjectSource | null>(null);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => () => {
    // An in-flight upload retains its source lease until the request settles.
    if (uploadingSource.current !== file) releaseSource(file);
  }, [file]);
  const [prepare, setPrepare] = useState(false);
  const [translationMode, setTranslationMode] = useState<
    "standard" | "best_of_three"
  >("standard");
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
  const { data: savedConfig, error: configError } = useQuery({
    queryKey: ["config", pid],
    queryFn: () => api.getConfig(pid!),
    enabled: !!pid,
  });
  const savedPipeline = savedConfig?.effective.pipeline as
    | Record<string, unknown>
    | undefined;
  const displayedMode = pid
    ? String(savedPipeline?.translation_mode ?? "standard")
    : translationMode;
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
    mutationFn: async () => {
      if (!file || fileError)
        throw new Error(fileError || tr("createProject.sourceRequired"));
      uploadingSource.current = file;
      try {
        return await api.createProject(
          {
            name: name.trim(),
            source_lang: source,
            target_lang: target,
            prepare: !subtitle && prepare,
            translation_mode: subtitle ? "standard" : translationMode,
            pdf_backend: extension === "pdf" && pdfBackend ? pdfBackend : null,
          },
          file,
        );
      } finally {
        uploadingSource.current = null;
        if (!mounted.current) releaseSource(file);
      }
    },
    onSuccess: (p) => {
      queryClient.setQueryData(["project", p.id], p);
      void queryClient.invalidateQueries({ queryKey: ["projects"] });
      if (mounted.current) navigate(`/projects/${p.id}`);
    },
    onError: () => {
      // Opaque native grants are single-use, even when an upload fails.
      if (file && "upload" in file) setSourceConsumed(true);
    },
  });
  const resume = useMutation({
    mutationFn: () => api.resume(pid!),
    onSuccess: () =>
      queryClient.invalidateQueries({ queryKey: ["project", pid] }),
  });
  const start = useMutation({
    mutationFn: () => api.translate(pid!),
    onSuccess: () => {
      if (mounted.current) navigate(`/projects/${pid}`);
    },
  });
  const sameLanguage = source !== "auto" && source === target;
  const locked = !!pid || create.isPending;
  const interrupted =
    project?.status === "error" || project?.status === "paused";
  const filename = file?.name || project?.source_meta?.original_filename;
  const pending = create.isPending || resume.isPending || start.isPending;
  const close = () => {
    if (pending) return;
    if (location.state?.fromAppNavigation || location.state?.fromProjectList)
      navigate(-1);
    else navigate("/", { replace: true });
  };

  return (
    <Dialog
      open
      title={tr("common.createProject")}
      description={tr("createProject.introduction")}
      onClose={close}
      closeDisabled={pending}
      initialFocus={nameRef}
      returnFocus={() =>
        document.getElementById(location.state?.returnFocusId || "create-project-trigger")
      }
      className="max-w-3xl"
    >
      <div className="space-y-4">
        <ErrorNotice
          error={
            capsError ||
            projectError ||
            configError ||
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
                ref={nameRef}
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
                  onValueChange={setSource}
                  className="mt-2"
                >
                  <SelectItem value="auto">
                    {tr("progress.detectAutomatically")}
                  </SelectItem>
                  {caps?.languages
                    .filter((l) => l.code !== "auto")
                    .map((l) => (
                      <SelectItem key={l.code} value={l.code}>
                        {languageName(l.code, l.name, locale)} ({l.code})
                      </SelectItem>
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
                  onValueChange={setTarget}
                  className="mt-2"
                >
                  {caps?.languages
                    .filter((l) => l.code !== "auto")
                    .map((l) => (
                      <SelectItem key={l.code} value={l.code}>
                        {languageName(l.code, l.name, locale)} ({l.code})
                      </SelectItem>
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
              size={file?.size}
              extensions={extensions}
              disabled={locked}
              onSelectFile={(file) => {
                setFile(file);
                setSourceConsumed(false);
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
                  onValueChange={(value) =>
                    setPdfBackend(value as typeof pdfBackend)
                  }
                >
                  <SelectItem value="">{tr("createProject.serverDefault")}</SelectItem>
                  <SelectItem value="mineru">MinerU</SelectItem>
                  <SelectItem value="babeldoc">BabelDOC</SelectItem>
                </Select>
              </div>
            )}
          </CardContent>
        </Card>
        {!subtitle && project?.fmt !== "srt" && (!pid || savedPipeline) && (
          <Card>
            <CardContent className="p-5 space-y-3">
              <fieldset disabled={locked} className="min-w-0">
                <legend className="mb-3 font-medium">
                  {tr("createProject.translationMode")}
                </legend>
                <div className="grid gap-3 sm:grid-cols-2">
                  {(["standard", "best_of_three"] as const).map((mode) => (
                    <label
                      key={mode}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-lg border p-4",
                        "transition-colors focus-within:ring-2 focus-within:ring-ring",
                        displayedMode === mode
                          ? "border-primary bg-primary/5"
                          : "hover:bg-muted/50",
                        locked && "cursor-default opacity-75",
                      )}
                    >
                      <input
                        type="radio"
                        name="translation-mode"
                        value={mode}
                        checked={displayedMode === mode}
                        onChange={() => setTranslationMode(mode)}
                        aria-label={tr(
                          mode === "standard"
                            ? "createProject.standardMode"
                            : "createProject.precisionMode",
                        )}
                        className="mt-1 shrink-0 accent-primary"
                      />
                      <span className="min-w-0 space-y-1">
                        <span className="block text-sm font-medium">
                          {tr(
                            mode === "standard"
                              ? "createProject.standardMode"
                              : "createProject.precisionMode",
                          )}
                        </span>
                        <span className="block text-sm text-muted-foreground">
                          {tr(
                            mode === "standard"
                              ? "createProject.standardHelp"
                              : "createProject.precisionHelp",
                          )}
                        </span>
                      </span>
                    </label>
                  ))}
                </div>
              </fieldset>
              {displayedMode === "best_of_three" && (
                <p className="text-xs text-muted-foreground">
                  {tr("createProject.precisionPolish")} {tr("createProject.precisionCost")}
                </p>
              )}
            </CardContent>
          </Card>
        )}
        <div className="space-y-4">
          {!pid && !subtitle && (
            <div className="flex items-start gap-3">
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
              <div className="flex flex-wrap gap-3">
                <Button variant="outline" disabled={pending} onClick={close}>
                  {tr("common.cancel")}
                </Button>
                <Button
                  onClick={() => create.mutate()}
                  disabled={
                    !name.trim() ||
                    !file ||
                    sourceConsumed ||
                    !!fileError ||
                    sameLanguage ||
                    !caps ||
                    create.isPending
                  }
                  className="w-full sm:w-auto"
                >
                  {create.isPending
                    ? tr("createProject.uploading")
                    : tr("common.createProject")}
                </Button>
              </div>
              {sourceConsumed && (
                <p role="status" className="text-xs text-muted-foreground">
                  {tr("createProject.selectSourceAgain")}
                </p>
              )}
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
        </div>
      </div>
    </Dialog>
  );
}
