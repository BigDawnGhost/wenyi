import { useI18n } from "@/i18n";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { skipToken, useIsMutating, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy, type AnalysisPayload } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Label, Textarea } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/misc";

const styleFields = [
  ["genre", "style.genre"],
  ["tone", "style.tone"],
  ["narration", "style.narration"],
  ["pacing", "style.pacing"],
  ["register", "style.register"],
  ["dialogue_style", "style.dialogueStyle"],
  ["rhetoric", "style.rhetoric"],
] as const;
const styleKeys = [...styleFields.map(([key]) => key), "style_guide"];

export default function StylePage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState("style");
  // Session-only, project-scoped drafts survive navigation without browser storage.
  const { data: draft = {} } = useQuery<Record<string, string>>({
    queryKey: ["analysis-draft", pid],
    queryFn: skipToken,
    initialData: {},
    gcTime: Infinity,
  });
  const setDraft = (update: (current: Record<string, string>) => Record<string, string>) =>
    qc.setQueryData<Record<string, string>>(["analysis-draft", pid], (current) => update(current || {}));
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid),
    refetchInterval: 3000,
  });
  const busy = isProjectBusy(project?.status);
  const { data, error } = useQuery({
    queryKey: ["analysis", pid],
    queryFn: () => api.getAnalysis(pid),
    enabled: !!pid,
  });

  const analysis = (data?.analysis || {}) as Record<string, unknown>;
  const digests = data?.chapter_digests || [];

  const saveKey = ["analysis-save", pid];
  const saving = useIsMutating({ mutationKey: saveKey, exact: true }) > 0;
  const save = useMutation({
    mutationKey: saveKey,
    mutationFn: (
      { projectId, analysis }: { projectId: string; analysis: Record<string, unknown> },
    ) => api.updateAnalysis(projectId, analysis),
    onSuccess: (_, { projectId, analysis: saved }) => {
      qc.setQueryData<AnalysisPayload>(["analysis", projectId], (current) =>
        current ? { ...current, analysis: saved } : current,
      );
      qc.setQueryData<Record<string, string>>(["analysis-draft", projectId], (current) =>
        Object.fromEntries(
          Object.entries(current || {}).filter(([key, value]) => saved[key] !== value),
        ),
      );
      qc.invalidateQueries({ queryKey: ["analysis", projectId] });
      toast.success(tr("style.saved"));
    },
  });

  const readOnly = busy || saving || !data;
  const valueFor = (key: string) => draft[key] ?? String(analysis[key] ?? "");
  const editField = (key: string, value: string) =>
    setDraft((current) => {
      const next = { ...current };
      if (value === String(analysis[key] ?? "")) delete next[key];
      else next[key] = value;
      return next;
    });
  const saveFields = (keys: string[]) => {
    if (qc.isMutating({ mutationKey: saveKey, exact: true })) return;
    save.mutate({
      projectId: pid,
      analysis: {
        ...analysis,
        ...Object.fromEntries(keys.filter((key) => key in draft).map((key) => [key, draft[key]])),
      },
    });
  };

  return (
    <>
      <PageHeader
        title={tr("common.styleSynopsis")}
        subtitle={tr("style.preparationResultsCanBeEditedToGuide")}
      />
      <PageContainer>
        <ErrorNotice error={error || save.error} />
        {busy && (
          <p className="text-sm text-muted-foreground mb-4">
            {tr("style.styleAndSynopsisAreReadOnlyWhile")}
          </p>
        )}
        <Tabs value={tab} onValueChange={setTab}>
          <TabsList className="flex w-fit max-w-full flex-wrap [&_button]:whitespace-nowrap">
            <TabsTrigger value="style">{tr("style.styleAnalysis")}</TabsTrigger>
            <TabsTrigger value="synopsis">
              {tr("style.bookSynopsis")}
            </TabsTrigger>
            <TabsTrigger value="digests">
              {tr("style.chapterSummaries")}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="style" className="mt-4 space-y-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{tr("style.styleOverview")}</CardTitle>
                <Button
                  size="sm"
                  onClick={() => saveFields(styleKeys)}
                  disabled={readOnly || !styleKeys.some((key) => key in draft)}
                >
                  {tr("common.save")}
                </Button>
              </CardHeader>
              <CardContent>
                <div className="grid md:grid-cols-3 gap-4">
                  {styleFields.map(([key, label]) => (
                    <div key={key} className="min-w-0 space-y-2">
                      <Label htmlFor={`style-${key}`}>{tr(label)}</Label>
                      <Textarea
                        id={`style-${key}`}
                        disabled={readOnly}
                        value={valueFor(key)}
                        onChange={(e) => editField(key, e.target.value)}
                      />
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>{tr("style.styleGuide")}</CardTitle>
              </CardHeader>
              <CardContent>
                <Textarea
                  aria-label={tr("style.styleGuide")}
                  disabled={readOnly}
                  className="min-h-[160px]"
                  value={valueFor("style_guide")}
                  onChange={(e) => editField("style_guide", e.target.value)}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="synopsis" className="mt-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{tr("style.wholeBookSynopsis")}</CardTitle>
                <Button
                  size="sm"
                  onClick={() => saveFields(["book_synopsis"])}
                  disabled={readOnly || !("book_synopsis" in draft)}
                >
                  {tr("common.save")}
                </Button>
              </CardHeader>
              <CardContent>
                <Textarea
                  aria-label={tr("style.wholeBookSynopsis")}
                  disabled={readOnly}
                  className="min-h-[220px]"
                  value={valueFor("book_synopsis")}
                  onChange={(e) => editField("book_synopsis", e.target.value)}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="digests" className="mt-4">
            <Card>
              <CardContent className="p-0">
                <div
                  aria-hidden="true"
                  className="hidden grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-4 border-b p-4 text-xs font-medium text-muted-foreground lg:grid"
                >
                  <span>{tr("common.chapter")}</span>
                  <span>{tr("common.summary")}</span>
                </div>
                <dl
                  aria-label={tr("style.chapterSummaries")}
                  className="divide-y text-sm"
                >
                  {digests.map((d) => (
                    <div
                      key={d.index}
                      className="grid gap-3 p-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,2fr)] lg:gap-4"
                    >
                      <dt className="min-w-0 font-medium [overflow-wrap:anywhere]">
                        {d.title?.trim() || tr("common.untitledChapter")}
                      </dt>
                      <dd className="min-w-0 text-muted-foreground">
                        <DigestEditor
                          pid={pid}
                          index={d.index}
                          value={d.digest || ""}
                          disabled={busy}
                        />
                      </dd>
                    </div>
                  ))}
                </dl>
                {digests.length === 0 && (
                  <p className="p-8 text-center text-sm text-muted-foreground">
                    {tr("style.noChapterSummariesYetEnableBookUnderstanding")}
                  </p>
                )}
              </CardContent>
            </Card>
          </TabsContent>
        </Tabs>
      </PageContainer>
    </>
  );
}

function DigestEditor({
  pid,
  index,
  value,
  disabled,
}: {
  pid: string;
  index: number;
  value: string;
  disabled: boolean;
}) {
  const { t: tr } = useI18n();
  const qc = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const save = useMutation({
    mutationFn: () => api.updateDigest(pid, index, draft),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["analysis", pid] });
      setEditing(false);
      toast.success(tr("style.chapterSummarySaved"));
    },
  });
  if (!editing)
    return (
      <button
        disabled={disabled}
        className="w-full text-left whitespace-pre-wrap [overflow-wrap:anywhere] hover:text-foreground"
        onClick={() => {
          setDraft(value);
          setEditing(true);
        }}
      >
        {value || tr("style.clickToAddASummary")}
      </button>
    );
  return (
    <div className="space-y-2">
      <ErrorNotice error={save.error} />
      <Textarea
        aria-label={tr("style.chapterSummary", { chapter: index + 1 })}
        className="min-h-32"
        disabled={disabled || save.isPending}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <div className="flex flex-wrap gap-2">
        <Button
          size="sm"
          disabled={disabled || save.isPending}
          onClick={() => save.mutate()}
        >
          {tr("style.saveSummary")}
        </Button>
        <Button
          size="sm"
          variant="ghost"
          disabled={save.isPending}
          onClick={() => setEditing(false)}
        >
          {tr("common.cancel")}
        </Button>
      </div>
    </div>
  );
}
