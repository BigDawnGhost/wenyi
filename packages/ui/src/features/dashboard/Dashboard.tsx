import { StatusBadge } from "@/components/StatusBadge";
import { useI18n } from "@/i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { Link } from "react-router-dom";
import { Brand, PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { Button, buttonVariants } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { ErrorNotice } from "@/components/ui/data";
import { api, type Project } from "@/lib/api";
import { cn } from "@/lib/utils";
import { LoaderCircle, Plus, Settings2, Trash2 } from "lucide-react";

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
    <div className="flex h-dvh flex-col overflow-hidden">
      <header className="flex h-16 shrink-0 items-center border-b px-4 sm:px-6">
        <Brand />
      </header>
      <main className="min-h-0 flex-1 overflow-y-auto">
        <div className="mx-auto max-w-7xl">
          <PageHeader
            title={tr("dashboard.myProjects")}
            subtitle={tr("dashboard.openAProjectToViewItsProgress")}
          />
          <PageContainer className="px-4 pb-[calc(10rem+env(safe-area-inset-bottom))] sm:px-6">
            <ErrorNotice error={error} />
            {isLoading ? (
              <p className="text-sm text-muted-foreground">
                {tr("dashboard.loading")}
              </p>
            ) : !projects?.length ? (
              <p className="py-24 text-center text-sm text-muted-foreground">
                {tr("dashboard.noProjectsYet")}
              </p>
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
        </div>
      </main>
      <nav
        aria-label={tr("navigation.global")}
        className="fixed bottom-[calc(1.5rem+env(safe-area-inset-bottom))] right-[calc(1.5rem+env(safe-area-inset-right))] z-20 flex flex-col gap-3"
      >
        <Link
          to="/projects/new"
          aria-label={tr("common.createProject")}
          title={tr("common.createProject")}
          className={cn(
            buttonVariants({ size: "icon" }),
            "h-12 w-12 rounded-full shadow-lg",
          )}
        >
          <Plus className="h-5 w-5" aria-hidden="true" />
        </Link>
        <Link
          to="/settings"
          aria-label={tr("settings.title")}
          title={tr("settings.title")}
          className={cn(
            buttonVariants({ variant: "outline", size: "icon" }),
            "h-12 w-12 rounded-full shadow-lg",
          )}
        >
          <Settings2 className="h-5 w-5" aria-hidden="true" />
        </Link>
      </nav>
    </div>
  );
}
