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
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [name, setName] = useState("");
  const [source, setSource] = useState("auto");
  const [target, setTarget] = useState("zh");
  const [template, setTemplate] = useState("标准翻译");
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
      toast.success("原文已上传，正在后台解析");
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
        title="创建项目"
        subtitle="选择语言和流程，上传原文，确认解析结果后开始翻译"
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
            <h2 className="font-medium">1. 项目与语言</h2>
            <div>
              <Label htmlFor="project-name">项目名称</Label>
              <Input
                id="project-name"
                value={name}
                disabled={!!pid}
                onChange={(e) => setName(e.target.value)}
                className="mt-2"
                placeholder="例如：短篇小说英译"
              />
            </div>
            <div className="grid gap-4 sm:grid-cols-2">
              <div>
                <Label htmlFor="source-language">源语言</Label>
                <Select
                  id="source-language"
                  value={source}
                  disabled={!!pid}
                  onChange={(e) => setSource(e.target.value)}
                  className="mt-2"
                >
                  <option value="auto">自动检测</option>
                  {caps?.languages
                    .filter((l) => l.code !== "auto")
                    .map((l) => (
                      <option key={l.code} value={l.code}>
                        {l.name}（{l.code}）
                      </option>
                    ))}
                </Select>
              </div>
              <div>
                <Label htmlFor="target-language">目标语言</Label>
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
                        {l.name}（{l.code}）
                      </option>
                    ))}
                </Select>
              </div>
            </div>
            {sameLanguage && (
              <ErrorNotice error="源语言与目标语言相同，请选择不同的目标语言。" />
            )}
            <div>
              <Label htmlFor="workflow-template">翻译流程</Label>
              <Select
                id="workflow-template"
                value={template}
                disabled={!!pid}
                onChange={(e) => setTemplate(e.target.value)}
                className="mt-2"
              >
                {templates?.map((t) => (
                  <option key={t.name} value={t.name}>
                    {t.name} — {t.description}
                  </option>
                ))}
              </Select>
              <p className="text-xs text-muted-foreground mt-2">
                默认开启全书预理解、润色、全书审校与自动修复。快速出稿适合初稿。字幕自动使用独立流程。
              </p>
            </div>
            {!pid ? (
              <Button
                onClick={() => create.mutate()}
                disabled={
                  !name.trim() || sameLanguage || !caps || create.isPending
                }
              >
                {create.isPending ? "创建中…" : "创建并配置"}
              </Button>
            ) : (
              <p className="text-sm text-muted-foreground">
                项目已创建。初始化后更换语言或原文内容需新建项目。
                <Link
                  className="text-primary underline ml-2"
                  to={`/projects/${pid}/settings`}
                >
                  项目配置与模型设置
                </Link>
              </p>
            )}
          </CardContent>
        </Card>
        {pid && (
          <Card>
            <CardContent className="p-5 space-y-4">
              <h2 className="font-medium">2. 上传原文</h2>
              <p className="text-sm text-muted-foreground">
                支持 {caps?.input_formats.join(" / ")}。PDF 解析服务、批次 Token
                预算和模型可在上传前通过项目配置调整。
              </p>
              <Input
                aria-label="上传原文"
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
                    ? "正在上传…"
                    : "正在解析原文，完成后自动显示预览。可以离开此页，通过项目进度查看结果。"}
                </p>
              )}
              {preview && (
                <div className="rounded border p-4 space-y-3">
                  <h3 className="font-medium">{preview.title}</h3>
                  <p className="text-sm">
                    {preview.fmt.toUpperCase()} ·{" "}
                    {preview.fmt === "srt"
                      ? `${preview.total_word_count} 条字幕`
                      : `${preview.chapter_count} 章 · ${preview.total_word_count} 段文本`}
                  </p>
                  <details>
                    <summary className="cursor-pointer text-sm">
                      查看解析结构
                    </summary>
                    <ol className="mt-2 max-h-56 overflow-auto text-sm space-y-2">
                      {preview.chapters.map((c) => (
                        <li key={c.index}>
                          {c.index + 1}. {c.title || "未命名"}{" "}
                          <span className="text-muted-foreground">
                            （{c.word_count} 段）
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
                  {start.isPending ? "启动中…" : "开始翻译"}
                </Button>
                <Link to={`/projects/${pid}`}>
                  <Button variant="outline">进入项目</Button>
                </Link>
              </div>
            </CardContent>
          </Card>
        )}
      </PageContainer>
    </>
  );
}
