import { useQuery } from "@tanstack/react-query";
import { Link, PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { api } from "@/lib/api";
import { Plus } from "lucide-react";

const STATUS_LABEL: Record<string, string> = {
  created: "已创建", preparing: "准备中", translating: "翻译中",
  paused: "已暂停", postprocessing: "译后处理", done: "已完成", error: "错误",
};

export default function Dashboard() {
  const { data: projects, isLoading } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
  });

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
              <Link key={p.id} to={`/projects/${p.id}`}>
                <Card className="hover:border-primary/40 transition-colors h-full">
                  <CardContent className="p-5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="font-medium truncate">{p.name}</div>
                        <div className="text-xs text-muted-foreground truncate mt-0.5">
                          {p.title || "（未上传）"}
                        </div>
                      </div>
                      <Badge variant="secondary">{STATUS_LABEL[p.status] || p.status}</Badge>
                    </div>
                    <div className="flex items-center gap-3 mt-4 text-xs text-muted-foreground">
                      <span>{p.source_lang || "?"} → {p.target_lang || "zh"}</span>
                      {p.fmt && <span>· {p.fmt}</span>}
                      {p.created_at && <span>· {new Date(p.created_at).toLocaleDateString()}</span>}
                    </div>
                  </CardContent>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </PageContainer>
    </>
  );
}
