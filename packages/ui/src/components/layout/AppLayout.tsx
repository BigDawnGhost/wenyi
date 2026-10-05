import { useI18n } from "@/i18n";
import { useEffect, useState } from "react";
import { Link, Outlet, useParams } from "react-router-dom";
import {
  PanelLeftClose,
  PanelLeftOpen,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { NavigationLink, ProjectNavigation } from "./Navigation";
import { navEntries } from "@/routes/manifest";
import { RouteGate } from "@/routes/RouteGate";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { platform } from "@/platform";

const emblemUrl = new URL("../../assets/wenyi-emblem.png", import.meta.url)
  .href;
const sidebarStorageKey = "wenyi.sidebarCollapsed";
// Remembers the last opened project so the project stratum stays visible on
// global routes; falls back to the first project when nothing is remembered.
const lastProjectKey = "wenyi.lastProject";

function Brand() {
  const { t } = useI18n();
  return (
    <Link
      to="/"
      className="inline-flex items-center gap-1.5 rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <img
        src={emblemUrl}
        alt=""
        width={36}
        height={36}
        className="h-9 w-9 shrink-0 object-contain grayscale dark:invert"
      />
      <span
        className="translate-y-0.5 text-[22px] font-normal leading-none tracking-wide"
        style={{
          fontFamily:
            "'DreamHanWenyi', Georgia, 'Times New Roman', 'Noto Serif CJK SC', 'Songti SC', SimSun, serif",
        }}
      >
        {t("appLayout.wenyi")}
      </span>
    </Link>
  );
}

export function AppLayout() {
  const { t: tr } = useI18n();
  const { pid } = useParams();
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
  const { data: project } = useQuery({
    queryKey: ["project", recalledPid],
    queryFn: () => api.getProject(recalledPid!),
    enabled: !!recalledPid,
  });

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
          className={slot === "action" ? "w-full border" : undefined}
        />
      ));

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden md:flex-row">
      <aside
        className={cn(
          "flex min-h-0 shrink-0 flex-col border-b bg-card md:border-b-0 md:border-r",
          collapsed ? "md:w-16" : "md:w-60",
        )}
      >
        <div
          data-slot="sidebar.logo-row"
          className="flex min-h-14 shrink-0 items-center justify-between gap-2 pl-6 pr-3 pt-[10px]"
        >
          <div className={cn("min-w-0", collapsed && "md:hidden")}>
            <Brand />
          </div>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className={cn(
              "h-8 w-8 shrink-0 text-muted-foreground",
              collapsed && "md:mx-auto",
            )}
            aria-label={toggleLabel}
            title={toggleLabel}
            aria-expanded={!collapsed}
            aria-controls="sidebar-navigation"
            onClick={toggleSidebar}
          >
            {collapsed ? (
              <PanelLeftOpen className="h-4 w-4" aria-hidden="true" />
            ) : (
              <PanelLeftClose className="h-4 w-4" aria-hidden="true" />
            )}
          </Button>
        </div>
        <div
          id="sidebar-navigation"
          data-slot="sidebar.strata"
          className={cn(
            "min-h-0 md:flex md:flex-1 md:flex-col",
            collapsed && "hidden",
          )}
        >
          <div
            data-slot="sidebar.action"
            className={cn("shrink-0", collapsed ? "p-2" : "px-3 pt-3 pb-1")}
          >
            {renderGlobal("action")}
          </div>
          <nav
            data-slot="sidebar.panels"
            aria-label={tr("navigation.global")}
            className={cn(
              "flex shrink-0 flex-wrap gap-1 md:block md:space-y-1",
              collapsed ? "p-2" : "px-3 py-1",
            )}
          >
            {renderGlobal("panel")}
          </nav>
          <div
            data-slot="sidebar.region"
            className="relative min-h-0 md:flex md:flex-col md:flex-1"
          >
            <div
              className={cn(
                "min-h-0 overflow-y-auto md:max-h-none md:flex-1",
                recalledPid && "max-h-[35vh]",
                recalledPid && (collapsed ? "p-2" : "p-3"),
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
            className={cn("shrink-0", collapsed ? "p-2" : "px-3 pt-1 pb-3")}
          >
            {renderGlobal("foot")}
          </div>
        </div>
      </aside>
      <main className="flex-1 min-h-0 min-w-0 overflow-y-auto">
        {/* The route gate keeps the shell mounted while a lazy chunk loads and
            confines route failures to the content area. */}
        <RouteGate>
          <Outlet />
        </RouteGate>
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
    <div className="flex flex-wrap items-start justify-between gap-4 border-b px-4 sm:px-6 py-4">
      <div className="min-w-0 flex-1 basis-64 [overflow-wrap:anywhere]">
        <h1 className="text-lg font-semibold">{title}</h1>
        {subtitle && (
          <p className="text-sm text-muted-foreground mt-0.5">{subtitle}</p>
        )}
      </div>
      {actions && (
        <div className="flex flex-wrap items-center gap-2">{actions}</div>
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
  return <div className={cn("p-6", className)}>{children}</div>;
}
