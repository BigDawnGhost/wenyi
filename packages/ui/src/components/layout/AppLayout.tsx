import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { Link, Outlet, useLocation, useParams } from "react-router-dom";
import { PanelLeftClose, PanelLeftOpen } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { SelectDismissScope } from "@/components/ui/select";
import { NavigationLink, ProjectNavigation } from "./Navigation";
import { navEntries } from "@/routes/manifest";
import { RouteGate } from "@/routes/RouteGate";
import { NotificationToaster } from "@/components/NotificationToaster";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { platform } from "@/platform";

const emblemUrl = new URL("../../assets/wenyi-emblem.png", import.meta.url)
  .href;
const sidebarStorageKey = "wenyi.sidebarCollapsed";
// Remembers the last opened project so the project stratum stays visible on
// global routes; falls back to the first project when nothing is remembered.
const lastProjectKey = "wenyi.lastProject";

export function Brand() {
  const { t } = useI18n();
  return (
    <Link
      to="/"
      className="inline-flex items-center gap-1.5 whitespace-nowrap rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <img
        src={emblemUrl}
        alt=""
        width={36}
        height={36}
        className="h-9 w-9 shrink-0 object-contain grayscale dark:invert"
      />
      <span className="brand-wordmark translate-y-0.5 text-[20px] font-normal leading-none tracking-wide">
        {t("appLayout.wenyi")}
      </span>
    </Link>
  );
}

export function AppLayout() {
  const { t: tr } = useI18n();
  const { pid } = useParams();
  const location = useLocation();
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return platform().preferences.get(sidebarStorageKey) === "true";
    } catch {
      return false;
    }
  });
  const toggleSidebar = () => {
    const next = !collapsed;
    setCollapsed(next);
    try {
      platform().preferences.set(sidebarStorageKey, String(next));
    } catch {
      // Keep navigation usable when browser storage is unavailable.
    }
  };
  const toggleLabel = tr(
    collapsed ? "navigation.expandSidebar" : "navigation.collapseSidebar",
  );
  const [storedPid, setStoredPid] = useState<string | null>(() => {
    try {
      return platform().preferences.get(lastProjectKey) || null;
    } catch {
      return null;
    }
  });
  useEffect(() => {
    if (!pid) return;
    setStoredPid(pid);
    try {
      platform().preferences.set(lastProjectKey, pid);
    } catch {
      // Keep recall session-only when storage is unavailable.
    }
  }, [pid]);
  // On global routes the list validates the remembered project and provides
  // the "first project" default; project routes never pay for the extra fetch.
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.listProjects,
    enabled: !pid,
  });
  const recalledPid =
    pid ??
    (projects.data
      ? storedPid && projects.data.some((p) => p.id === storedPid)
        ? storedPid
        : projects.data[0]?.id ?? null
      : storedPid);
  const { data: projectDetails } = useQuery({
    queryKey: ["project", recalledPid],
    queryFn: () => api.getProject(recalledPid!),
    enabled: !!recalledPid,
  });
  // Only use metadata for this identity; retaining the previous project's
  // format would build the wrong menu for the newly selected project.
  const project = projectDetails ?? projects.data?.find((item) => item.id === recalledPid);

  // One layered column: logo row → primary action → global panels → scrollable
  // project region → footer, instead of three bordered blocks.
  const globalNav = navEntries
    .filter((entry) => entry.group === "global")
    .sort((a, b) => a.order - b.order);
  const renderGlobal = (slot: "action" | "panel" | "foot") =>
    globalNav
      .filter((entry) =>
        slot === "panel" ? !entry.slot || entry.slot === "panel" : entry.slot === slot,
      )
      .map((entry) => (
        <NavigationLink
          key={entry.to}
          to={entry.to}
          icon={entry.icon}
          label={entry.label}
          collapsed={collapsed}
          end={entry.end}
          prefetch={entry.loader}
          id={slot === "action" ? "create-project-trigger" : undefined}
          state={slot === "action" ? { fromAppNavigation: true } : undefined}
          className={slot === "action" ? "w-full border" : undefined}
        />
      ));

  return (
    <div className="flex h-dvh w-full overflow-hidden">
      <aside
        data-slot="sidebar"
        data-collapsed={collapsed}
        className={cn(
          "flex min-h-0 shrink-0 flex-col border-r bg-card transition-[width] duration-sidebar ease-sidebar",
          collapsed ? "w-16" : "w-60",
        )}
      >
        <div
          data-slot="sidebar.logo-row"
          className={cn(
            "flex min-h-14 shrink-0 items-center justify-between gap-2 pl-5 pr-3 pt-[10px] transition-[padding,gap] duration-sidebar ease-sidebar",
            collapsed && "gap-0 pl-4 pr-[15px]",
          )}
        >
          <div
            className={cn(
              "min-w-0 max-w-40 overflow-hidden transition-[max-width,opacity,transform,visibility] duration-sidebar ease-sidebar",
              collapsed && "invisible max-w-0 -translate-x-2 opacity-0",
            )}
          >
            <Brand />
          </div>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="relative h-8 w-8 shrink-0 text-muted-foreground"
            aria-label={toggleLabel}
            title={toggleLabel}
            aria-expanded={!collapsed}
            aria-controls="sidebar-navigation"
            onClick={toggleSidebar}
          >
            <PanelLeftClose
              className={cn(
                "absolute h-4 w-4 transition-[opacity,transform] duration-sidebar ease-sidebar",
                collapsed && "rotate-180 opacity-0",
              )}
              aria-hidden="true"
            />
            <PanelLeftOpen
              className={cn(
                "absolute h-4 w-4 transition-[opacity,transform] duration-sidebar ease-sidebar",
                !collapsed && "-rotate-180 opacity-0",
              )}
              aria-hidden="true"
            />
          </Button>
        </div>
        <div
          id="sidebar-navigation"
          data-slot="sidebar.strata"
          className="flex min-h-0 flex-1 flex-col"
        >
          <div
            data-slot="sidebar.action"
            className={cn(
              "shrink-0 px-3 pt-3 pb-1 transition-[padding] duration-sidebar ease-sidebar",
              collapsed && "p-2",
            )}
          >
            {renderGlobal("action")}
          </div>
          <nav
            data-slot="sidebar.panels"
            aria-label={tr("navigation.global")}
            className={cn(
              "shrink-0 space-y-1 px-3 py-1 transition-[padding] duration-sidebar ease-sidebar",
              collapsed && "p-2",
            )}
          >
            {renderGlobal("panel")}
          </nav>
          <div
            data-slot="sidebar.region"
            className="relative flex min-h-0 flex-1 flex-col"
          >
            <div
              className={cn(
                "min-h-0 flex-1 overflow-y-auto transition-[padding] duration-sidebar ease-sidebar",
                recalledPid && "p-3",
                recalledPid && collapsed && "p-2",
              )}
            >
              {recalledPid && (
                <ProjectNavigation
                  key={recalledPid}
                  pid={recalledPid}
                  format={project?.fmt}
                  name={project?.name}
                  collapsed={collapsed}
                />
              )}
            </div>
          </div>
          <div
            data-slot="sidebar.footer"
            className={cn(
              "shrink-0 px-3 pt-1 pb-3 transition-[padding] duration-sidebar ease-sidebar",
              collapsed && "p-2",
            )}
          >
            {renderGlobal("foot")}
          </div>
        </div>
      </aside>
      <NotificationToaster />
      <main
        data-slot="content"
        className="relative flex-1 min-h-0 min-w-0 overflow-auto"
      >
        {/* The route gate keeps the shell mounted while a lazy chunk loads and
            confines route failures to the content area. Preserve desktop columns
            in narrow windows and scroll only this pane (704px content + 48px padding). */}
        <div className="min-h-full min-w-[752px]">
          <RouteGate>
            <SelectDismissScope dismissKey={`${location.key}:${location.hash}`}>
              <Outlet />
            </SelectDismissScope>
          </RouteGate>
        </div>
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: React.ReactNode;
}) {
  return (
    <div
      data-slot="page.header"
      className="relative flex flex-wrap items-center justify-between gap-4 border-b px-6 py-4"
    >
      <div className="flex min-w-0 flex-1 basis-64 flex-wrap items-center gap-x-3 gap-y-1 [overflow-wrap:anywhere]">
        <h1 className="text-lg font-semibold">{title}</h1>
        {subtitle && (
          <p className="text-sm text-muted-foreground">{subtitle}</p>
        )}
      </div>
      {actions && (
        <div className="flex max-w-full flex-wrap items-center gap-2">
          {actions}
        </div>
      )}
    </div>
  );
}

export function PageContainer({
  children,
  className,
}: {
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div data-slot="page.container" className={cn("p-6", className)}>
      {children}
    </div>
  );
}
