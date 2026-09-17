import { useI18n } from "@/i18n";
import {
  defaultWorkflowTemplate,
  workflowTemplateLabel,
  languageName,
} from "@/i18n/labels";
import { useEffect, useState } from "react";
import { useNavigate, useSearchParams, Link } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { api, isProjectBusy, type UploadPreview } from "@/lib/api";

export default function CreateProject() {
  const { t: tr, locale } = useI18n();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [name, setName] = useState("");
  const [source, setSource] = useState("auto");
  const [target, setTarget] = useState("zh");
  const [template, setTemplate] = useState(defaultWorkflowTemplate);
  const [pid, setPid] = useState<string | null>(searchParams.get("project"));
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [parsing, setParsing] = useState(false);
  const { data: caps, error: capsError } = useQuery({
    queryKey: ["capabilities"],
    queryFn: api.capabilities,
  });
  const { data: templates } = useQuery({
    queryKey: ["templates"],
    queryFn: api.listTemplates,
  });
  const { data: project, error: projectError } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid!),
    enabled: !!pid,
    refetchInterval: parsing ? 1500 : false,
  });
  const { data: parsed } = useQuery({
    queryKey: ["preview", pid],
    queryFn: () => api.getPreview(pid!),
    enabled: !!pid && (parsing || !!project?.fmt),
    retry: false,
    refetchInterval: parsing ? 1500 : false,
  });
  useEffect(() => {
    if (project && pid) {
      setName(project.name);
      setSource(project.source_lang || "auto");
      setTarget(project.target_lang || "zh");
      if (project.status === "parsing") setParsing(true);
    }
  }, [project, pid]);
  useEffect(() => {
    if (parsed) {
      setPreview(parsed);
      setParsing(false);
    }
  }, [parsed]);
  useEffect(() => {
    if (project?.status === "error") setParsing(false);
  }, [project?.status]);
  const create = useMutation({
    mutationFn: () =>
      api.createProject({
        name: name.trim(),
        source_lang: source,
        target_lang: target,
        strategy: { template },
      }),
    onSuccess: (p) => {
      setPid(p.id);
      setSearchParams({ project: p.id }, { replace: true });
    },
  });
  const upload = useMutation({
    mutationFn: (file: File) => api.uploadSource(pid!, file),
    onSuccess: () => {
      setPreview(null);
      setParsing(true);
      toast.success(tr("createProject.sourceUploadedParsingInTheBackground"));
    },
  });
  const start = useMutation({
    mutationFn: () => api.translate(pid!),
    onSuccess: () => navigate(`/projects/${pid}`),
  });
  const sameLanguage = source !== "auto" && source === target;
  const disabled =
    parsing || upload.isPending || isProjectBusy(project?.status);
  const accepts = (caps?.input_formats || [])
    .flatMap((f) =>
      f === "markdown"
        ? [".md", ".markdown"]
        : f === "html"
          ? [".html", ".htm"]
          : [`.${f}`],
    )
    .join(",");

  return (
    <>
      <PageHeader
        title={tr("common.createProject")}
        subtitle={tr("createProject.chooseLanguagesAndAWorkflowUploadThe")}
      />
      <PageContainer className="max-w-3xl space-y-4">
        <ErrorNotice
          error={
            capsError ||
            projectError ||
            create.error ||
            upload.error ||
            start.error ||
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
                disabled={!!pid}
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
                  disabled={!!pid}
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
                  disabled={!!pid || !caps}
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
            <div>
              <Label htmlFor="workflow-template">
                {tr("createProject.translationWorkflow")}
              </Label>
              <Select
                id="workflow-template"
                value={template}
                disabled={!!pid}
                onChange={(e) => setTemplate(e.target.value)}
                className="mt-2"
              >
                {templates?.map((t) => (
                  <option key={t.name} value={t.name}>
                    {workflowTemplateLabel(t.name, t.description, tr)}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground mt-2">
                {tr(
                  "createProject.bookUnderstandingPolishingWholeBookReviewAnd",
                )}
              </p>
            </div>
            {!pid ? (
              <Button
                onClick={() => create.mutate()}
                disabled={
                  !name.trim() || sameLanguage || !caps || create.isPending
                }
              >
                {create.isPending
                  ? tr("createProject.creating")
                  : tr("createProject.createConfigure")}
              </Button>
            ) : (
              <p className="text-sm text-muted-foreground">
                {tr(
                  "createProject.projectCreatedChangingLanguagesOrSourceContent",
                )}
                <Link
                  className="text-primary underline ml-2"
                  to={`/projects/${pid}/settings`}
                >
                  {tr("createProject.projectSettingsModels")}
                </Link>
              </p>
            )}
          </CardContent>
        </Card>
        {pid && (
          <Card>
            <CardContent className="p-5 space-y-4">
              <h2 className="font-medium">{tr("createProject.uploadStep")}</h2>
              <p className="text-sm text-muted-foreground">
                {tr("createProject.uploadHelp", {
                  formats: caps?.input_formats.join(" / ") || "—",
                })}
              </p>
              <Input
                aria-label={tr("createProject.uploadSource")}
                type="file"
                accept={accepts}
                disabled={disabled || !!preview}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) upload.mutate(file);
                }}
              />
              {disabled && (
                <p role="status" className="text-sm">
                  {upload.isPending
                    ? tr("createProject.uploading")
                    : tr("createProject.parsingTheSourceAPreviewWillAppear")}
                </p>
              )}
              {preview && (
                <div className="rounded border p-4 space-y-3">
                  <h3 className="font-medium">{preview.title}</h3>
                  <p className="text-sm">
                    {preview.fmt.toUpperCase()} ·{" "}
                    {preview.fmt === "srt"
                      ? tr("createProject.subtitleCues", {
                          count: preview.total_word_count,
                        })
                      : tr("createProject.chaptersParagraphs", {
                          chapters: preview.chapter_count,
                          count: preview.total_word_count,
                        })}
                  </p>
                  <details>
                    <summary className="cursor-pointer text-sm">
                      {tr("createProject.viewParsedStructure")}
                    </summary>
                    <ol className="mt-2 max-h-56 overflow-auto text-sm space-y-2">
                      {preview.chapters.map((c) => (
                        <li key={c.index}>
                          {c.index + 1}.{" "}
                          {c.title || tr("createProject.untitled")}{" "}
                          <span className="text-muted-foreground">
                            {tr("createProject.paragraphCount", {
                              count: c.word_count,
                            })}
                          </span>
                        </li>
                      ))}
                    </ol>
                  </details>
                </div>
              )}
              <div className="flex gap-3">
                <Button
                  onClick={() => start.mutate()}
                  disabled={!preview || disabled || start.isPending}
                >
                  {start.isPending
                    ? tr("createProject.starting")
                    : tr("common.startTranslation")}
                </Button>
                <Link to={`/projects/${pid}`}>
                  <Button variant="outline">
                    {tr("createProject.openProject")}
                  </Button>
                </Link>
              </div>
            </CardContent>
          </Card>
        )}
      </PageContainer>
    </>
  );
}
