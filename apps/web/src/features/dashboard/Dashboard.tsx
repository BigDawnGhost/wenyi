import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Link, PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api, type Project } from "@/lib/api";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";

const STATUS_LABEL: Record<string, string> = {
  created: "已创建", preparing: "准备中", translating: "翻译中",
  paused: "已暂停", postprocessing: "译后处理", done: "已完成", error: "错误",
};

export default function Dashboard() {
  const queryClient = useQueryClient();
  const { data: projects, isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });
  const deleteProject = useMutation({
    mutationFn: (project: Project) => api.deleteProject(project.id),
    onSuccess: async (_, project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success(`已删除项目“${project.name}”`);
    },
    onError: (error) => toast.error(`删除失败：${error.message}`),
  });

  const requestDelete = (project: Project) => {
    if (window.confirm(`确认删除项目“${project.name}”？此操作无法撤销。`)) {
      deleteProject.mutate(project);
    }
  };

  return (
    <>
      <PageHeader
        title="我的项目"
        subtitle="点击项目查看翻译进度，或创建新项目"
        actions={
          <Link to="/projects/new">
            <Button><Plus className="h-4 w-4" /> 创建项目</Button>
          </Link>
        }
      />
      <PageContainer>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : !projects?.length ? (
          <Card>
            <CardContent className="py-16 text-center text-muted-foreground">
              <p>还没有项目。</p>
              <Link to="/projects/new" className="inline-block mt-3">
                <Button><Plus className="h-4 w-4" /> 创建第一个项目</Button>
              </Link>
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {projects.map((p) => (
              <Card key={p.id} className="relative h-full transition-colors hover:border-primary/40">
                <Link
                  to={`/projects/${p.id}`}
                  aria-label={`打开项目 ${p.name}`}
                  className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <CardContent className="relative pointer-events-none p-5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="font-medium truncate">{p.name}</div>
                      <div className="text-xs text-muted-foreground truncate mt-0.5">
                        {p.title || "（未上传）"}
                      </div>
                    </div>
                    <div className="relative z-10 flex shrink-0 items-center gap-1 pointer-events-auto">
                      <Badge variant="secondary">{STATUS_LABEL[p.status] || p.status}</Badge>
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground hover:text-destructive"
                        aria-label={`删除项目 ${p.name}`}
                        title="删除项目"
                        disabled={deleteProject.isPending}
                        onClick={() => requestDelete(p)}
                      >
                        {deleteProject.isPending && deleteProject.variables?.id === p.id
                          ? <LoaderCircle className="h-4 w-4 animate-spin" />
                          : <Trash2 className="h-4 w-4" />}
                      </Button>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 mt-4 text-xs text-muted-foreground">
                    <span>{p.source_lang || "?"} → {p.target_lang || "zh"}</span>
                    {p.fmt && <span>· {p.fmt}</span>}
                    {p.created_at && <span>· {new Date(p.created_at).toLocaleDateString()}</span>}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </PageContainer>
    </>
  );
}
