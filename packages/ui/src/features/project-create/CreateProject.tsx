import { useI18n } from "../../i18n";
import { languageName } from "../../i18n/labels";
import { useEffect, useRef, useState } from "react";
import { ArrowRight } from "lucide-react";
import { useLocation, useNavigate, useSearchParams, Link } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Button } from "../../components/ui/button";
import { Input, Label, Switch } from "../../components/ui/form";
import { Dialog } from "../../components/ui/misc";
import { Select, SelectItem } from "../../components/ui/select";
import { ErrorNotice } from "../../components/ui/data";
import { api, isProjectBusy } from "../../api/client";
import type { ProjectInput } from "../../api/types";
import { SourcePreview } from "./SourcePreview";
import { SourceFilePicker } from "./SourceFilePicker";
import { ProjectLocationPicker } from "./ProjectLocationPicker";
import { releaseSource, type ProjectSource } from "../../platform";

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
  // Null means the installation's default library. Kept out of the project query
  // cache: it applies to the project being created, not to one that exists.
  const [folder, setFolder] = useState<string | null>(null);
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
    NonNullable<ProjectInput["translation_mode"]>
  >("standard");
  const [promptGenre, setPromptGenre] = useState<
    NonNullable<ProjectInput["prompt_genre"]>
  >("literary");
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
  const displayedGenre = pid
    ? String(savedPipeline?.prompt_genre ?? "literary")
    : promptGenre;
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
            prepare,
            translation_mode: translationMode,
            prompt_genre: promptGenre,
            pdf_backend: extension === "pdf" && pdfBackend ? pdfBackend : null,
            // Omitted rather than sent as null when no folder was chosen: the
            // request then means "the default", exactly as it did before a
            // location could be chosen at all.
            ...(folder ? { location: folder } : {}),
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
  // Closing never cancels a request that is already in flight. The dialog is
  // only a view of it, so dismissing stays available and the result still
  // arrives in the query cache; `mounted` below decides whether to redirect.
  const close = () => {
    if (location.state?.fromAppNavigation || location.state?.fromProjectList)
      navigate(-1);
    else navigate("/", { replace: true });
  };

  const languages = (caps?.languages || []).filter((l) => l.code !== "auto");
  const createDisabled =
    !name.trim() ||
    !file ||
    sourceConsumed ||
    !!fileError ||
    sameLanguage ||
    !caps ||
    create.isPending;
  const footer = pid ? (
    <>
      <Link to={`/projects/${pid}`}>
        <Button variant="outline">{tr("createProject.openProject")}</Button>
      </Link>
      {interrupted ? (
        <Button onClick={() => resume.mutate()} disabled={resume.isPending}>
          {tr("progress.resumeTask")}
        </Button>
      ) : (
        <Button
          onClick={() => start.mutate()}
          disabled={!preview || busy || start.isPending}
        >
          {start.isPending ? tr("createProject.starting") : tr("common.startTranslation")}
        </Button>
      )}
    </>
  ) : (
    <>
      {sourceConsumed && (
        <p role="status" className="mr-auto text-xs text-muted-foreground">
          {tr("createProject.selectSourceAgain")}
        </p>
      )}
      <Button variant="outline" onClick={close}>
        {tr(pending ? "common.close" : "common.cancel")}
      </Button>
      <Button onClick={() => create.mutate()} disabled={createDisabled}>
        {create.isPending ? tr("createProject.uploading") : tr("common.createProject")}
      </Button>
    </>
  );

  return (
    <Dialog
      open
      title={tr("common.createProject")}
      onClose={close}
      initialFocus={nameRef}
      returnFocus={() =>
        document.getElementById(location.state?.returnFocusId || "create-project-trigger")
      }
      className="max-w-2xl"
      footer={footer}
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
        <div className="space-y-2">
          <Label htmlFor="project-name">{tr("createProject.projectName")}</Label>
          <Input
            ref={nameRef}
            id="project-name"
            value={name}
            disabled={locked}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)] items-end gap-3 sm:grid-cols-[minmax(0,1fr)_auto_minmax(0,1fr)_minmax(0,1fr)]">
          <div className="min-w-0 space-y-2">
            <Label htmlFor="source-language">{tr("createProject.sourceLanguage")}</Label>
            <Select
              id="source-language"
              value={source}
              disabled={locked}
              onValueChange={setSource}
            >
              <SelectItem value="auto">{tr("progress.detectAutomatically")}</SelectItem>
              {languages.map((l) => (
                <SelectItem key={l.code} value={l.code}>
                  {languageName(l.code, l.name, locale)} ({l.code})
                </SelectItem>
              ))}
            </Select>
          </div>
          <ArrowRight
            className="mb-2.5 h-4 w-4 text-muted-foreground"
            aria-hidden="true"
          />
          <div className="min-w-0 space-y-2">
            <Label htmlFor="target-language">{tr("createProject.targetLanguage")}</Label>
            <Select
              id="target-language"
              value={target}
              disabled={locked || !caps}
              onValueChange={setTarget}
            >
              {languages.map((l) => (
                <SelectItem key={l.code} value={l.code}>
                  {languageName(l.code, l.name, locale)} ({l.code})
                </SelectItem>
              ))}
            </Select>
          </div>
          {(!pid || savedPipeline) && (
            <div className="col-span-3 min-w-0 space-y-2 sm:col-span-1">
              <Label htmlFor="prompt-genre">{tr("createProject.promptGenre")}</Label>
              <Select
                id="prompt-genre"
                value={displayedGenre}
                disabled={locked}
                onValueChange={(value) => setPromptGenre(value as typeof promptGenre)}
              >
                <SelectItem value="literary">{tr("createProject.literaryGenre")}</SelectItem>
                <SelectItem value="academic">{tr("createProject.academicGenre")}</SelectItem>
              </Select>
            </div>
          )}
        </div>
        {sameLanguage && (
          <ErrorNotice error={tr("createProject.theSourceAndTargetLanguagesAreThe")} />
        )}
        <SourceFilePicker
          filename={typeof filename === "string" ? filename : undefined}
          size={file?.size}
          extensions={extensions}
          formats={caps?.input_formats || []}
          disabled={locked}
          onSelectFile={(file) => {
            setFile(file);
            setName((current) =>
              current.trim() ? current : file.name.replace(/\.[^.]+$/, "").trim() || file.name,
            );
            setSourceConsumed(false);
            create.reset();
          }}
        />
        {!pid && extension === "pdf" && (
          <div className="space-y-2">
            <Label htmlFor="pdf-backend">{tr("settings.pdfParser")}</Label>
            <Select
              id="pdf-backend"
              value={pdfBackend}
              disabled={locked}
              onValueChange={(value) => setPdfBackend(value as typeof pdfBackend)}
            >
              <SelectItem value="">{tr("createProject.serverDefault")}</SelectItem>
              <SelectItem value="mineru">MinerU</SelectItem>
              <SelectItem value="babeldoc">BabelDOC</SelectItem>
            </Select>
          </div>
        )}
        {/* Existing projects keep their directory; this picker never moves them. */}
        {!pid && (
          <ProjectLocationPicker
            location={folder}
            disabled={locked}
            onChange={setFolder}
          />
        )}
        {(!pid || savedPipeline) && (
          <div className="flex flex-wrap items-center gap-x-8 gap-y-3">
            <div className="flex items-center gap-2">
              <Switch
                id="precision-translation"
                checked={displayedMode === "three_draft_synthesis"}
                disabled={locked}
                onCheckedChange={(checked) =>
                  setTranslationMode(checked ? "three_draft_synthesis" : "standard")
                }
                className="peer"
              />
              <Label
                htmlFor="precision-translation"
                className="cursor-pointer peer-disabled:cursor-default peer-disabled:opacity-50"
              >
                {tr("createProject.precisionMode")}
              </Label>
            </div>
            {!pid && (
              <div className="flex items-center gap-2">
                <Switch
                  id="prepare-source"
                  checked={prepare}
                  disabled={locked}
                  onCheckedChange={setPrepare}
                  className="peer"
                />
                <Label
                  htmlFor="prepare-source"
                  className="cursor-pointer peer-disabled:cursor-default peer-disabled:opacity-50"
                >
                  {tr("createProject.prepareSource")}
                </Label>
              </div>
            )}
          </div>
        )}
        {pid && (
          <Link
            className="inline-block text-sm text-muted-foreground underline underline-offset-4"
            to={`/projects/${pid}/settings`}
          >
            {tr("common.projectSettings")}
          </Link>
        )}
        {busy && (
          <p role="status" className="text-sm text-muted-foreground">
            {project?.status === "preparing"
              ? tr("createProject.preparingSource")
              : tr("createProject.parsingTheSourceAPreviewWillAppear")}
          </p>
        )}
        {preview && <SourcePreview preview={preview} />}
      </div>
    </Dialog>
  );
}
