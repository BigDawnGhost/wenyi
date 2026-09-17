import { StatusBadge } from "@/components/StatusBadge";
import { useI18n } from "@/i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Link, PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/data";
import { api, type Project } from "@/lib/api";
import { LoaderCircle, Plus, Trash2 } from "lucide-react";

export default function Dashboard() {
  const { t: tr, locale } = useI18n();
  const queryClient = useQueryClient();
  const {
    data: projects,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
    refetchInterval: 5000,
  });
  const deleteProject = useMutation({
    mutationFn: (project: Project) => api.deleteProject(project.id),
    onSuccess: async (_, project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success(tr("dashboard.projectDeleted", { name: project.name }));
    },
    onError: (error) =>
      toast.error(
        tr("dashboard.couldNotDeleteProject", { error: error.message }),
      ),
  });

  const requestDelete = (project: Project) => {
    if (
      window.confirm(
        tr("dashboard.deleteProjectThisCannotBeUndone", { name: project.name }),
      )
    ) {
      deleteProject.mutate(project);
    }
  };

  return (
    <>
      <PageHeader
        title={tr("dashboard.myProjects")}
        subtitle={tr("dashboard.openAProjectToViewItsProgress")}
        actions={
          <Link to="/projects/new">
            <Button>
              <Plus className="h-4 w-4" />
              {tr("common.createProject")}
            </Button>
          </Link>
        }
      />
      <PageContainer>
        <ErrorNotice error={error} />
        {isLoading ? (
          <p className="text-sm text-muted-foreground">
            {tr("dashboard.loading")}
          </p>
        ) : !projects?.length ? (
          <Card>
            <CardContent className="py-16 text-center text-muted-foreground">
              <p>{tr("dashboard.noProjectsYet")}</p>
              <Link to="/projects/new" className="inline-block mt-3">
                <Button>
                  <Plus className="h-4 w-4" />
                  {tr("dashboard.createYourFirstProject")}
                </Button>
              </Link>
            </CardContent>
          </Card>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 lg:grid-cols-3">
            {projects.map((p) => (
              <Card
                key={p.id}
                className="relative h-full transition-colors hover:border-primary/40"
              >
                <Link
                  to={`/projects/${p.id}`}
                  aria-label={tr("dashboard.openProject", { name: p.name })}
                  className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                />
                <CardContent className="relative pointer-events-none p-5">
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="font-medium truncate">{p.name}</div>
                      <div className="text-xs text-muted-foreground truncate mt-0.5">
                        {p.title || tr("dashboard.noSourceUploaded")}
                      </div>
                    </div>
                    <div className="relative z-10 flex shrink-0 items-center gap-1 pointer-events-auto">
                      <StatusBadge status={p.status} />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-7 w-7 text-muted-foreground hover:text-destructive"
                        aria-label={tr("dashboard.deleteProject", {
                          name: p.name,
                        })}
                        title={tr("dashboard.deleteAction")}
                        disabled={deleteProject.isPending}
                        onClick={() => requestDelete(p)}
                      >
                        {deleteProject.isPending &&
                        deleteProject.variables?.id === p.id ? (
                          <LoaderCircle className="h-4 w-4 animate-spin" />
                        ) : (
                          <Trash2 className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                  </div>
                  <div className="flex items-center gap-3 mt-4 text-xs text-muted-foreground">
                    <span>
                      {p.source_lang || "?"} → {p.target_lang || "zh"}
                    </span>
                    {p.fmt && <span>· {p.fmt}</span>}
                    {p.created_at && (
                      <span>
                        · {new Date(p.created_at).toLocaleDateString(locale)}
                      </span>
                    )}
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
