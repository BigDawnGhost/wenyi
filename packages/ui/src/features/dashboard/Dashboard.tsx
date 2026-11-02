import { StatusBadge } from "../../components/StatusBadge";
import { useRef, useState } from "react";
import { useI18n } from "../../i18n";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "../../lib/toast";
import { errorMessage } from "../../lib/error-codes";
import { Link, Outlet, useNavigate } from "react-router-dom";
import { PageContainer, PageHeader } from "../../components/layout/AppLayout";
import { Button } from "../../components/ui/button";
import { LazyBoundary } from "../../routes/LazyBoundary";
import { Card, CardContent } from "../../components/ui/card";
import { ErrorNotice } from "../../components/ui/data";
import { Dialog } from "../../components/ui/misc";
import { api, isProjectBusy } from "../../api/client";
import type { Project } from "../../api/types";
import { platform } from "../../platform";
import { FolderPlus, LoaderCircle, Trash2 } from "lucide-react";

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
  // shown beside the button rather than in a toast that disappears.
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
          library && (
            <Button
              variant="outline"
              disabled={importProject.isPending}
              onClick={() => importProject.mutate()}
            >
              {importProject.isPending ? (
                <LoaderCircle className="h-4 w-4 animate-spin" />
              ) : (
                <FolderPlus className="h-4 w-4" />
              )}
              {tr("dashboard.importProject")}
            </Button>
          )
        }
      />
      <PageContainer>
        <ErrorNotice error={error} />
        {importFailure && (
          <div
            role="alert"
            className="mb-3 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm"
          >
            <p className="font-medium">{tr("dashboard.couldNotImportProject")}</p>
            <p className="mt-1 text-muted-foreground [overflow-wrap:anywhere]">
              {importFailure}
            </p>
          </div>
        )}
        {isLoading ? (
          <p className="text-sm text-muted-foreground">
            {tr("dashboard.loading")}
          </p>
        ) : !projects?.length ? (
          <p className="py-24 text-center text-sm text-muted-foreground">
            {tr("dashboard.noProjectsYet")}
          </p>
        ) : (
          <div className="grid grid-cols-3 gap-3">
            {projects.map((p) => {
              // A project on a drive that is not mounted cannot be opened, so it
              // is shown with what is wrong instead of failing on selection.
              const unavailable = p.available === false;
              return (
                <Card
                  key={p.id}
                  className={`relative h-full transition-colors ${
                    unavailable
                      ? "opacity-60"
                      : "hover:border-primary/40"
                  }`}
                >
                  {!unavailable && (
                    <Link
                      to={`/projects/${p.id}`}
                      aria-label={tr("dashboard.openProject", { name: p.name })}
                      className="absolute inset-0 rounded-lg focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
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
                          className="h-7 w-7 text-muted-foreground hover:text-destructive"
                          aria-label={tr("dashboard.deleteProject", {
                            name: p.name,
                          })}
                          title={tr("dashboard.deleteAction")}
                          disabled={deleteProject.isPending}
                          onClick={() => setPendingDelete(p)}
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
              );
            })}
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
              {tr(deleteProject.isPending ? "common.close" : "common.cancel")}
            </Button>
            <Button
              variant="destructive"
              disabled={deleteProject.isPending || !pendingDelete}
              onClick={() => {
                if (pendingDelete && !deleteProject.isPending) {
                  deleteProject.mutate(pendingDelete);
                }
              }}
            >
              {deleteProject.isPending && <LoaderCircle className="h-4 w-4 animate-spin" />}
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
