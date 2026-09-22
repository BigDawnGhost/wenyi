import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { api, isProjectBusy } from "@/lib/api";
import { PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/form";
import { ErrorNotice } from "@/components/ui/data";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/misc";

export default function StylePage() {
  const { t: tr } = useI18n();
  const { pid = "" } = useParams();
  const qc = useQueryClient();
  const [tab, setTab] = useState("style");
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
  const characters = (analysis.characters as Record<string, string>[]) || [];
  const styleGuide = String(analysis.style_guide || "");
  const synopsis = String(analysis.book_synopsis || "");
  const digests = data?.chapter_digests || [];

  const save = useMutation({
    mutationFn: (a: Record<string, unknown>) => api.updateAnalysis(pid, a),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["analysis", pid] });
      toast.success(tr("style.saved"));
    },
  });

  const [guideDraft, setGuideDraft] = useState<string>(styleGuide);
  const [synopsisDraft, setSynopsisDraft] = useState<string>(synopsis);
  // Copy incoming data into the editable draft.
  useEffect(() => {
    setGuideDraft(styleGuide);
    setSynopsisDraft(synopsis);
  }, [data]);

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
            <TabsTrigger value="characters">
              {tr("style.characters")}
            </TabsTrigger>
            <TabsTrigger value="synopsis">
              {tr("style.bookSynopsis")}
            </TabsTrigger>
            <TabsTrigger value="digests">
              {tr("style.chapterSummaries")}
            </TabsTrigger>
          </TabsList>

          <TabsContent value="style" className="mt-4 space-y-4">
            <Card>
              <CardHeader>
                <CardTitle>{tr("style.styleOverview")}</CardTitle>
              </CardHeader>
              <CardContent>
                <div className="grid md:grid-cols-3 gap-4 text-sm">
                  {(
                    [
                      [tr("style.genre"), analysis.genre],
                      [tr("style.tone"), analysis.tone],
                      [tr("style.narration"), analysis.narration],
                      [tr("style.pacing"), analysis.pacing],
                      [tr("style.dialogueStyle"), analysis.dialogue_style],
                      [tr("style.rhetoric"), analysis.rhetoric],
                    ] as [string, unknown][]
                  ).map(([k, v]) => (
                    <div key={k}>
                      <div className="text-xs text-muted-foreground">{k}</div>
                      <div className="font-medium">
                        {String(v ?? "") || "—"}
                      </div>
                    </div>
                  ))}
                </div>
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{tr("style.styleGuide")}</CardTitle>
                <Button
                  size="sm"
                  onClick={() =>
                    save.mutate({ ...analysis, style_guide: guideDraft })
                  }
                  disabled={save.isPending || busy}
                >
                  {tr("common.save")}
                </Button>
              </CardHeader>
              <CardContent>
                <Textarea
                  disabled={busy}
                  className="min-h-[160px]"
                  value={guideDraft}
                  onChange={(e) => setGuideDraft(e.target.value)}
                />
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="characters" className="mt-4">
            <Card>
              <CardContent className="p-0">
                <table className="w-full text-sm">
                  <thead className="border-b text-xs text-muted-foreground">
                    <tr>
                      {[
                        tr("style.characterName"),
                        tr("style.translatedName"),
                        tr("style.description"),
                        tr("common.gender"),
                        tr("style.firstAppearance"),
                      ].map((h) => (
                        <th key={h} className="text-left p-3 font-medium">
                          {h}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {characters.map((c, i) => (
                      <tr key={i} className="border-b last:border-0">
                        <td className="p-3 font-medium">{c.source}</td>
                        <td className="p-3">{c.target}</td>
                        <td className="p-3 text-muted-foreground">
                          {c.note || "—"}
                        </td>
                        <td className="p-3">{c.gender || "—"}</td>
                        <td className="p-3 text-muted-foreground">
                          {c.first_seen ?? "—"}
                        </td>
                      </tr>
                    ))}
                    {characters.length === 0 && (
                      <tr>
                        <td
                          colSpan={5}
                          className="p-8 text-center text-muted-foreground text-sm"
                        >
                          {tr("style.noCharacterDataYetEnableStyleAnalysis")}
                        </td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </CardContent>
            </Card>
          </TabsContent>

          <TabsContent value="synopsis" className="mt-4">
            <Card>
              <CardHeader className="flex-row items-center justify-between">
                <CardTitle>{tr("style.wholeBookSynopsis")}</CardTitle>
                <Button
                  size="sm"
                  onClick={() =>
                    save.mutate({ ...analysis, book_synopsis: synopsisDraft })
                  }
                  disabled={save.isPending || busy}
                >
                  {tr("common.save")}
                </Button>
              </CardHeader>
              <CardContent>
                <Textarea
                  disabled={busy}
                  className="min-h-[220px]"
                  value={synopsisDraft}
                  onChange={(e) => setSynopsisDraft(e.target.value)}
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
