import { StatusBadge } from "../../components/StatusBadge";
import { useRef, useState } from "react";
import { useI18n } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { errorMessage } from "../../lib/error-codes";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button, buttonVariants } from "../../components/ui/button";
import { LazyBoundary } from "../../routes/LazyBoundary";
import { loadCreateProject } from "../../routes/manifest";
import { Card, CardContent } from "../../components/ui/card";
import { ErrorNotice } from "../../components/ui/data";
import { Dialog, EmptyState } from "../../components/ui/misc";
import { api, isProjectBusy } from "../../api/client";
import type { Project } from "../../api/types";
import { platform } from "../../platform";
import { FolderInput, FolderPlus, Trash2 } from "lucide-react";

export default function Dashboard() {
  const { t: tr, locale } = useI18n();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [pendingDelete, setPendingDelete] = useState<Project | null>(null);
  const cancelDeleteRef = useRef<HTMLButtonElement>(null);
  const library = platform().capabilities.library;
  const {
    data: projects,
    isLoading,
    error,
  } = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
    refetchInterval: (query) =>
      query.state.data?.some((project) => isProjectBusy(project.status)) ? 5000 : false,
  });
  const deleteProject = useMutation({
    mutationFn: (project: Project) => api.deleteProject(project.id),
    onSuccess: async (_, project) => {
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      setPendingDelete(null);
      toast.success(tr("dashboard.projectDeleted", { name: project.name }));
    },
    onError: (error) =>
      toast.error(
        tr("dashboard.couldNotDeleteProject", { error: errorMessage(error, tr) }),
      ),
  });
  // A refused import says why: which file is missing, or that the original no
  // longer matches. The reason is the whole point of the entry point, so it is
  // shown in the project list rather than in a toast that disappears.
  const [importFailure, setImportFailure] = useState<string | null>(null);
  const importProject = useMutation({
    mutationFn: async () => {
      const chosen = await library!.choose();
      if (!chosen) return null;
      return api.importProject(chosen);
    },
    onSuccess: async (imported) => {
      if (!imported) return;
      setImportFailure(null);
      await queryClient.invalidateQueries({ queryKey: ["projects"] });
      toast.success(tr("dashboard.projectImported", { name: imported.name }));
      if (imported.source_changed) {
        toast.warning(tr("dashboard.sourceChangedAfterImport"));
      }
      // Routing stays in the interface: the host only adopts the folder.
      navigate(`/projects/${imported.id}`);
    },
    onError: (failure) => setImportFailure(errorMessage(failure, tr)),
  });

  return (
    <>
      <PageHeader
        title={tr("dashboard.myProjects")}
        actions={
          <Link
            id="create-project-trigger"
            to="/projects/new"
            state={{ fromProjectList: true }}
            className={buttonVariants({ variant: "outline" })}
            onPointerEnter={() => void loadCreateProject().catch(() => {})}
            onFocus={() => void loadCreateProject().catch(() => {})}
          >
            <FolderPlus className="h-4 w-4" aria-hidden="true" />
            {tr("common.createProject")}
          </Link>
        }
      />
      <PageContainer className="mx-auto w-full max-w-5xl">
        <ErrorNotice error={error} />
        {isLoading ? (
          // The skeleton mirrors the loaded card grid so loading keeps the same
          // geometry and the page does not jump when the projects arrive.
          <div role="status" aria-label={tr("dashboard.loading")}>
            <div aria-hidden="true" className="grid grid-cols-3 gap-4">
              {Array.from({ length: 6 }, (_, skeleton) => (
                <Card key={skeleton}>
                  <CardContent className="p-5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0 flex-1 space-y-2">
                        <div className="h-6 w-2/3 rounded bg-muted motion-safe:animate-pulse" />
                        <div className="h-4 w-1/3 rounded bg-muted motion-safe:animate-pulse" />
                      </div>
                      <div className="h-7 w-7 shrink-0 rounded bg-muted motion-safe:animate-pulse" />
                    </div>
                    <div className="mt-4 h-4 w-1/2 rounded bg-muted motion-safe:animate-pulse" />
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        ) : !projects?.length ? (
          <Card>
            <EmptyState className="py-24">{tr("dashboard.noProjectsYet")}</EmptyState>
          </Card>
        ) : (
          <div className="grid grid-cols-3 gap-4">
            {projects.map((p) => {
              // A project on a drive that is not mounted cannot be opened, so it
              // is shown with what is wrong instead of failing on selection.
              const unavailable = p.available === false;
              return (
                <Card
                  key={p.id}
                  className={
                    unavailable
                      ? "relative opacity-60"
                      : "relative cursor-pointer hover:shadow-e3"
                  }
                >
                  {!unavailable && (
                    <Link
                      to={`/projects/${p.id}`}
                      aria-label={tr("dashboard.openProject", { name: p.name })}
                      className="absolute inset-0 rounded-xl"
                    />
                  )}
                  <CardContent className="relative pointer-events-none p-5">
                    <div className="flex items-start justify-between gap-2">
                      <div className="min-w-0">
                        <div className="font-medium truncate">{p.name}</div>
                        <div className="text-xs text-muted-foreground truncate mt-0.5">
                          {unavailable
                            ? tr("dashboard.projectUnavailable")
                            : p.title || tr("dashboard.noSourceUploaded")}
                        </div>
                      </div>
                      <div className="relative z-10 flex shrink-0 items-center gap-1 pointer-events-auto">
                        <StatusBadge status={p.status} />
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="text-muted-foreground hover:text-destructive"
                          aria-label={tr("dashboard.deleteProject", {
                            name: p.name,
                          })}
                          title={tr("dashboard.deleteAction")}
                          aria-busy={
                            deleteProject.isPending && deleteProject.variables?.id === p.id
                              || undefined
                          }
                          onClick={() => setPendingDelete(p)}
                        >
                          <Trash2 className="h-4 w-4" />
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
              );
            })}
          </div>
        )}
        {library && (
          <div className="mt-4 space-y-2">
            <Button
              variant="ghost"
              className="text-muted-foreground"
              aria-busy={importProject.isPending || undefined}
              onClick={() => { if (!importProject.isPending) importProject.mutate(); }}
            >
              <FolderInput className="h-4 w-4" aria-hidden="true" />
              {tr("dashboard.importProject")}
            </Button>
            {importFailure && (
              <ErrorNotice>
                <p className="font-medium">{tr("dashboard.couldNotImportProject")}</p>
                <p className="mt-1 text-muted-foreground [overflow-wrap:anywhere]">
                  {importFailure}
                </p>
              </ErrorNotice>
            )}
          </div>
        )}
      </PageContainer>
      <Dialog
        open={pendingDelete !== null}
        onClose={() => setPendingDelete(null)}
        title={tr("dashboard.deleteAction")}
        initialFocus={cancelDeleteRef}
        footer={
          <>
            <Button
              ref={cancelDeleteRef}
              variant="outline"
              onClick={() => setPendingDelete(null)}
            >
              {/* Dismissing the dialog does not stop a delete that is already
                  running, so the label must not promise that it does. */}
              {tr("common.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={!pendingDelete}
              aria-busy={deleteProject.isPending || undefined}
              onClick={() => {
                if (pendingDelete && !deleteProject.isPending) {
                  deleteProject.mutate(pendingDelete);
                }
              }}
            >
              {/* The icon stays put while the deletion runs; swapping in a spinner
                  replaced the glyph and rotated it, which the footer never does. */}
              <Trash2 className="h-4 w-4 shrink-0" />
              {tr("dashboard.deleteAction")}
            </Button>
          </>
        }
      >
        <p className="text-sm">
          {tr("dashboard.deleteProjectThisCannotBeUndone", { name: pendingDelete?.name ?? "" })}
        </p>
      </Dialog>
      <LazyBoundary fallback={null}>
        <Outlet />
      </LazyBoundary>
    </>
  );
}
