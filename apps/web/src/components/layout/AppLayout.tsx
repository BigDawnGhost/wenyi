import { useI18n } from "@/i18n";
import { useState } from "react";
import { Link, Outlet, useNavigate, useParams } from "react-router-dom";
import {
  FolderPlus,
  LayoutDashboard,
  PanelLeftClose,
  PanelLeftOpen,
  Settings2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { NavigationLink, ProjectNavigation } from "./Navigation";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

const emblemUrl = new URL("../../assets/wenyi-emblem.png", import.meta.url)
  .href;
const sidebarStorageKey = "wenyi.sidebarCollapsed";

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
            "Georgia, 'Times New Roman', 'Noto Serif CJK SC', 'Songti SC', SimSun, serif",
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
      return localStorage.getItem(sidebarStorageKey) === "true";
    } catch {
      return false;
    }
  });
  const toggleSidebar = () => {
    const next = !collapsed;
    setCollapsed(next);
    try {
      localStorage.setItem(sidebarStorageKey, String(next));
    } catch {
      // Keep navigation usable when browser storage is unavailable.
    }
  };
  const toggleLabel = tr(
    collapsed ? "navigation.expandSidebar" : "navigation.collapseSidebar",
  );
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid!),
    enabled: !!pid,
  });

  return (
    <div className="flex h-screen w-full flex-col overflow-hidden md:flex-row">
      <aside
        className={cn(
          "flex min-h-0 shrink-0 flex-col border-b bg-card md:border-b-0 md:border-r",
          collapsed ? "md:w-16" : "md:w-60",
        )}
      >
        <div className="flex h-14 shrink-0 items-center justify-between gap-2 border-b px-3">
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
          className={cn(
            "min-h-0 md:flex md:flex-1 md:flex-col",
            collapsed && "hidden",
          )}
        >
          <div
            className={cn(
              "min-h-0 overflow-y-auto md:max-h-none md:flex-1",
              pid && "max-h-[35vh]",
              pid && (collapsed ? "p-2" : "p-3"),
            )}
          >
            {pid && (
              <ProjectNavigation
                key={pid}
                pid={pid}
                format={project?.fmt}
                name={project?.name}
                collapsed={collapsed}
              />
            )}
          </div>
          <nav
            aria-label={tr("navigation.global")}
            className={cn(
              "flex shrink-0 flex-wrap gap-1 border-t md:block md:space-y-1",
              collapsed ? "p-2" : "p-3",
            )}
          >
            <NavigationLink
              to="/"
              icon={LayoutDashboard}
              label="appLayout.projects"
              collapsed={collapsed}
              end
            />
            <NavigationLink
              to="/projects/new"
              icon={FolderPlus}
              label="common.createProject"
              collapsed={collapsed}
            />
            <NavigationLink
              to="/settings"
              icon={Settings2}
              label="settings.title"
              collapsed={collapsed}
            />
          </nav>
        </div>
      </aside>
      <main className="flex-1 min-h-0 min-w-0 overflow-y-auto">
        <Outlet />
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

export { Link, useNavigate };
