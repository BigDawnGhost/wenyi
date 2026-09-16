import {
  Link,
  NavLink,
  Outlet,
  useNavigate,
  useParams,
} from "react-router-dom";
import {
  BookOpenText,
  FolderPlus,
  LayoutDashboard,
  Languages,
  ListChecks,
  Download,
  ScrollText,
  Sparkles,
  Library,
  Settings2,
  Captions,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

export function AppLayout() {
  const { pid } = useParams();
  const { data: project } = useQuery({
    queryKey: ["project", pid],
    queryFn: () => api.getProject(pid!),
    enabled: !!pid,
  });

  const navItem = (to: string, icon: React.ReactNode, label: string) => (
    <NavLink
      to={to}
      end={to === "/" || to === `/projects/${pid}`}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2 rounded-md px-3 py-2 text-sm transition-colors",
          isActive
            ? "bg-accent text-accent-foreground font-medium"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )
      }
    >
      {icon}
      <span>{label}</span>
    </NavLink>
  );

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <aside className="hidden md:flex w-60 shrink-0 flex-col border-r bg-card">
        <div className="flex items-center gap-2 px-4 h-14 border-b">
          <BookOpenText className="h-5 w-5" />
          <span className="font-semibold">文译 Wenyi</span>
        </div>
        <nav className="flex-1 space-y-1 p-3 overflow-y-auto">
          {navItem("/", <LayoutDashboard className="h-4 w-4" />, "项目列表")}
          {navItem(
            "/projects/new",
            <FolderPlus className="h-4 w-4" />,
            "创建项目",
          )}
          {pid && (
            <>
              <div className="px-3 pt-4 pb-1 text-[11px] uppercase tracking-wider text-muted-foreground">
                当前项目
              </div>
              <div
                className="px-3 pb-1 text-xs text-muted-foreground truncate"
                title={project?.name}
              >
                {project?.name || pid}
              </div>
              {navItem(
                `/projects/${pid}`,
                <Sparkles className="h-4 w-4" />,
                "翻译进度",
              )}
              {project?.fmt !== "srt" && (
                <>
                  {navItem(
                    `/projects/${pid}/glossary`,
                    <Library className="h-4 w-4" />,
                    "术语表",
                  )}
                  {navItem(
                    `/projects/${pid}/style`,
                    <Languages className="h-4 w-4" />,
                    "风格 & 概要",
                  )}
                  {navItem(
                    `/projects/${pid}/review`,
                    <ListChecks className="h-4 w-4" />,
                    "全书审校与人工校阅",
                  )}
                </>
              )}
              {project?.fmt === "srt" &&
                navItem(
                  `/projects/${pid}/subtitles`,
                  <Captions className="h-4 w-4" />,
                  "字幕对照与编辑",
                )}
              {navItem(
                `/projects/${pid}/settings`,
                <Settings2 className="h-4 w-4" />,
                "项目配置与模型",
              )}
              {navItem(
                `/projects/${pid}/export`,
                <Download className="h-4 w-4" />,
                "导出",
              )}
              {navItem(
                `/projects/${pid}/events`,
                <ScrollText className="h-4 w-4" />,
                "事件日志",
              )}
            </>
          )}
        </nav>
      </aside>
      <main className="flex-1 min-w-0 overflow-y-auto">
        <div className="md:hidden flex gap-3 overflow-x-auto border-b p-3 text-sm">
          <Link to="/">项目列表</Link>
          <Link to="/projects/new">创建项目</Link>
          {pid && (
            <>
              <Link to={`/projects/${pid}`}>进度</Link>
              <Link
                to={`/projects/${pid}/${project?.fmt === "srt" ? "subtitles" : "review"}`}
              >
                校阅
              </Link>
              <Link to={`/projects/${pid}/settings`}>配置</Link>
              <Link to={`/projects/${pid}/export`}>导出</Link>
            </>
          )}
        </div>
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
      <div>
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
