import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { toast } from "sonner";
import { useDropzone } from "react-dropzone";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label, Select } from "@/components/ui/form";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/progress";
import { cn } from "@/lib/utils";
import { api, type UploadPreview } from "@/lib/api";
import { Check, ChevronLeft, ChevronRight, Upload as UploadIcon } from "lucide-react";

const LANGS = [
  ["auto", "自动检测"], ["ja", "日语"], ["en", "英语"], ["ko", "韩语"],
  ["ru", "俄语"], ["fr", "法语"], ["de", "德语"], ["es", "西班牙语"], ["pt", "葡萄牙语"],
];

const PRESETS = [
  { name: "快速出稿", desc: "速度优先，适合初稿或大批量", factor: 1 },
  { name: "标准翻译", desc: "质量与速度平衡（推荐）", factor: 2, recommended: true },
  { name: "精翻", desc: "质量优先，全部步骤开启", factor: 4 },
];

export default function CreateProject() {
  const navigate = useNavigate();
  const [step, setStep] = useState(1);
  const [name, setName] = useState("");
  const [sourceLang, setSourceLang] = useState("auto");
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<UploadPreview | null>(null);
  const [strategy, setStrategy] = useState<{ template?: string; steps?: Record<string, unknown> }>({ template: "标准翻译" });
  const [customOpen, setCustomOpen] = useState(false);
  const [pid, setPid] = useState<string | null>(null);

  const { data: stepsDef } = useQuery({ queryKey: ["steps"], queryFn: api.listSteps });

  // 创建项目（第一步进入时即建，后续步骤复用 pid）
  const ensureProject = useMutation({
    mutationFn: async () => pid || api.createProject({ name, source_lang: sourceLang, target_lang: "zh", strategy }).then((p) => p.id),
    onSuccess: (id) => setPid(id),
  });

  const upload = useMutation({
    mutationFn: ({ id, f }: { id: string; f: File }) => api.uploadSource(id, f),
    onSuccess: setPreview,
    onError: (e) => toast.error(`上传失败：${(e as Error).message}`),
  });

  const onDrop = (accepted: File[]) => {
    const f = accepted[0];
    if (!f) return;
    setFile(f);
    if (pid) upload.mutate({ id: pid, f });
  };
  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop, accept: { "application/epub+zip": [".epub"], "text/plain": [".txt", ".md"], "application/x-fictionbook": [".fb2"] }, maxFiles: 1,
  });

  const startTranslation = useMutation({
    mutationFn: async () => {
      const id = pid!;
      await api.translate(id, strategy);
      return id;
    },
    onSuccess: (id) => { toast.success("已开始翻译"); navigate(`/projects/${id}`); },
    onError: (e) => toast.error(`启动失败：${(e as Error).message}`),
  });

  const next = async () => {
    if (step === 1 && !pid) {
      if (!name.trim()) { toast.error("请填写项目名称"); return; }
      await ensureProject.mutateAsync();
    }
    if (step === 2 && pid && file && !preview) {
      await upload.mutateAsync({ id: pid, f: file });
    }
    setStep((s) => Math.min(4, s + 1));
  };

  return (
    <>
      <PageHeader title="创建项目" subtitle="四步完成配置并开始翻译" />
      <PageContainer className="max-w-3xl">
        <Stepper step={step} />
        <Card className="mt-4">
          <CardContent className="p-6">
            {step === 1 && (
              <div className="space-y-4">
                <div>
                  <Label>项目名称</Label>
                  <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="如《xxx》中文翻译" className="mt-1.5" />
                </div>
                <div className="grid grid-cols-2 gap-4">
                  <div>
                    <Label>源语言</Label>
                    <Select value={sourceLang} onChange={(e) => setSourceLang(e.target.value)} className="mt-1.5">
                      {LANGS.map(([v, l]) => <option key={v} value={v}>{l}</option>)}
                    </Select>
                  </div>
                  <div>
                    <Label>目标语言</Label>
                    <Select value="zh" disabled className="mt-1.5">
                      <option value="zh">简体中文</option>
                    </Select>
                  </div>
                </div>
              </div>
            )}

            {step === 2 && (
              <div className="space-y-4">
                {!preview ? (
                  <div {...getRootProps()} className={cn("border-2 border-dashed rounded-lg p-10 text-center cursor-pointer transition-colors", isDragActive ? "border-primary bg-accent/50" : "border-input hover:border-primary/40")}>
                    <input {...getInputProps()} />
                    <UploadIcon className="h-8 w-8 mx-auto text-muted-foreground" />
                    <p className="mt-2 text-sm">拖拽文件到此处，或点击选择</p>
                    <p className="mt-1 text-xs text-muted-foreground">支持 .epub / .fb2 / .txt / .md</p>
                  </div>
                ) : (
                  <div className="rounded-lg border bg-emerald-50 dark:bg-emerald-950/30 p-4 space-y-3">
                    <div className="flex items-center gap-2">
                      <Badge variant="success">解析成功</Badge>
                      <span className="text-sm font-medium">{file?.name}</span>
                    </div>
                    <div className="grid grid-cols-4 gap-3 text-sm">
                      <Field label="书名" value={preview.title} />
                      <Field label="格式" value={preview.fmt} />
                      <Field label="章节数" value={String(preview.chapter_count)} />
                      <Field label="总段落数" value={String(preview.total_word_count)} />
                    </div>
                    <details>
                      <summary className="text-xs text-muted-foreground cursor-pointer">查看目录结构</summary>
                      <div className="mt-2 max-h-48 overflow-auto text-xs space-y-1">
                        {preview.chapters.map((c) => (
                          <div key={c.index} className="flex justify-between">
                            <span className="truncate">{c.title || `第 ${c.index + 1} 章`}</span>
                            <span className="text-muted-foreground">{c.word_count} 段</span>
                          </div>
                        ))}
                      </div>
                    </details>
                  </div>
                )}
              </div>
            )}

            {step === 3 && (
              <StrategyStep
                strategy={strategy} setStrategy={setStrategy}
                stepsDef={stepsDef || []} customOpen={customOpen} setCustomOpen={setCustomOpen}
              />
            )}

            {step === 4 && (
              <div className="space-y-4">
                <div className="grid grid-cols-2 gap-3 text-sm">
                  <Field label="项目名称" value={name} />
                  <Field label="源语言" value={LANGS.find((l) => l[0] === sourceLang)?.[1] || sourceLang} />
                  <Field label="章节数" value={String(preview?.chapter_count ?? "—")} />
                  <Field label="总段落数" value={String(preview?.total_word_count ?? "—")} />
                  <Field label="翻译策略" value={strategy.template || "自定义"} />
                </div>
                <div className="rounded-md bg-sky-50 dark:bg-sky-950/30 p-3 text-sm text-sky-800 dark:text-sky-200">
                  预估耗时倍数：~{(PRESETS.find((p) => p.name === strategy.template)?.factor) || 2}x（相对快速出稿）
                </div>
              </div>
            )}
          </CardContent>
        </Card>

        <div className="flex items-center justify-between mt-4">
          <Button variant="ghost" disabled={step === 1} onClick={() => setStep((s) => s - 1)}>
            <ChevronLeft className="h-4 w-4" /> 上一步
          </Button>
          {step < 4 ? (
            <Button onClick={next} disabled={upload.isPending}>
              {upload.isPending ? "上传中…" : "下一步"} <ChevronRight className="h-4 w-4" />
            </Button>
          ) : (
            <Button onClick={() => startTranslation.mutate()} disabled={startTranslation.isPending || !preview}>
              {startTranslation.isPending ? "启动中…" : "开始翻译"}
            </Button>
          )}
        </div>
      </PageContainer>
    </>
  );
}

function Field({ label, value }: { label: string; value?: string | null }) {
  return (
    <div>
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="font-medium truncate">{value || "—"}</div>
    </div>
  );
}

function Stepper({ step }: { step: number }) {
  const labels = ["基本信息", "上传原文", "选择策略", "确认启动"];
  return (
    <div className="flex items-center gap-2">
      {labels.map((l, i) => {
        const n = i + 1;
        const active = step === n;
        const done = step > n;
        return (
          <div key={l} className="flex items-center gap-2">
            <div className={cn("flex h-6 w-6 items-center justify-center rounded-full text-xs", active ? "bg-primary text-primary-foreground" : done ? "bg-emerald-500 text-white" : "bg-muted text-muted-foreground")}>
              {done ? <Check className="h-3 w-3" /> : n}
            </div>
            <span className={cn("text-sm", active ? "font-medium" : "text-muted-foreground")}>{l}</span>
            {n < labels.length && <div className="w-8 h-px bg-border mx-1" />}
          </div>
        );
      })}
    </div>
  );
}

// 策略选择步骤：3 预设卡 + 自定义开关面板（依赖置灰）
function StrategyStep({ strategy, setStrategy, stepsDef, customOpen, setCustomOpen }: {
  strategy: { template?: string; steps?: Record<string, unknown> };
  setStrategy: (s: { template?: string; steps?: Record<string, unknown> }) => void;
  stepsDef: { id: string; name: string; category: string; always_on?: boolean; locked?: boolean; group?: string | null; depends_on?: string[]; description?: string | null; output?: string | null }[];
  customOpen: boolean; setCustomOpen: (b: boolean) => void;
}) {
  const usingTemplate = !!strategy.template;
  const customSteps = (strategy.steps || {}) as Record<string, boolean | number>;

  const setStepToggle = (id: string, val: boolean) => {
    setStrategy({ steps: { ...customSteps, [id]: val } });
  };
  const isDepBlocked = (deps: string[] = []) =>
    deps.some((d) => customSteps[d] === false || customSteps[d] === undefined);

  // 按 category 分组（自定义视图）
  const grouped = ["prepare", "per_chapter", "post_process"].map((cat) => ({
    cat, items: stepsDef.filter((s) => s.category === cat),
  }));

  return (
    <div className="space-y-4">
      <div className="grid md:grid-cols-3 gap-3">
        {PRESETS.map((p) => {
          const active = usingTemplate && strategy.template === p.name;
          return (
            <button key={p.name}
              onClick={() => setStrategy({ template: p.name })}
              className={cn("text-left rounded-lg border p-4 transition-colors", active ? "border-primary ring-1 ring-primary" : "hover:border-primary/40")}>
              <div className="flex items-center justify-between">
                <span className="font-medium">{p.name}</span>
                {p.recommended && <Badge variant="info">推荐</Badge>}
              </div>
              <p className="text-xs text-muted-foreground mt-1">{p.desc}</p>
              <p className="text-xs mt-2">~{p.factor}x 耗时</p>
            </button>
          );
        })}
      </div>

      <div>
        <button className="text-sm text-primary hover:underline" onClick={() => { setCustomOpen(!customOpen); if (!customOpen && !strategy.steps) setStrategy({ steps: { book_understanding: true, polish: true, punctuation_normalize: true, chapter_review: true, autofix: false, backtranslate: 0, consistency_qa: false } }); }}>
          {customOpen ? "收起自定义策略" : "自定义策略（展开配置）"}
        </button>
        {customOpen && (
          <div className="mt-3 rounded-lg border bg-muted/30 p-4 space-y-4">
            {grouped.map(({ cat, items }) => (
              <div key={cat}>
                <div className="text-xs uppercase tracking-wider text-muted-foreground mb-2">
                  {cat === "prepare" ? "翻译前准备" : cat === "per_chapter" ? "每章翻译" : "翻译后处理"}
                </div>
                <div className="space-y-1">
                  {items.map((s) => {
                    const blocked = !s.always_on && !s.locked && isDepBlocked(s.depends_on);
                    const checked = s.always_on || s.locked ? true : !!customSteps[s.id];
                    return (
                      <div key={s.id} className={cn("flex items-center justify-between rounded px-2 py-1.5", blocked && "opacity-50")}>
                        <div className="min-w-0">
                          <div className="text-sm flex items-center gap-2">
                            {s.name}
                            {s.locked && <Badge variant="secondary">必须</Badge>}
                          </div>
                          {(s.description || s.output) && (
                            <div className="text-xs text-muted-foreground">{s.description || `→ ${s.output}`}</div>
                          )}
                          {blocked && <div className="text-xs text-amber-600">⚠️ 依赖：{s.depends_on?.join(", ")}</div>}
                        </div>
                        <Switch
                          checked={checked}
                          disabled={!!s.locked || s.always_on || blocked}
                          onClick={() => setStepToggle(s.id, !checked)}
                        />
                      </div>
                    );
                  })}
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
