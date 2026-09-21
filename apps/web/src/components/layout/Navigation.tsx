import { NavLink } from "react-router-dom";
import {
  BookOpenCheck,
  Captions,
  Download,
  Languages,
  Library,
  ListChecks,
  ListTree,
  ScrollText,
  Settings2,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/utils";

export function NavigationLink({
  to,
  icon: Icon,
  label,
  end,
  collapsed = false,
}: {
  to: string;
  icon: LucideIcon;
  label: MessageKey;
  end?: boolean;
  collapsed?: boolean;
}) {
  const { t } = useI18n();
  return (
    <NavLink
      to={to}
      end={end}
      title={collapsed ? t(label) : undefined}
      className={({ isActive }) =>
        cn(
          "flex items-center rounded-md py-2 text-sm transition-colors",
          collapsed ? "justify-center px-2" : "gap-2 px-3",
          isActive
            ? "bg-accent text-accent-foreground font-medium"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
        )
      }
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span className={collapsed ? "sr-only" : undefined}>{t(label)}</span>
    </NavLink>
  );
}

export function ProjectNavigation({
  pid,
  format,
  name,
  collapsed = false,
}: {
  pid: string;
  format?: string | null;
  name?: string;
  collapsed?: boolean;
}) {
  const { t } = useI18n();
  const base = `/projects/${pid}`;
  const book = !!format && format !== "srt";
  const links = [
    ...(book
      ? [
          {
            path: "glossary",
            icon: Library,
            label: "common.glossary" as const,
          },
          {
            path: "style",
            icon: Languages,
            label: "common.styleSynopsis" as const,
          },
          {
            path: "contents",
            icon: ListTree,
            label: "contents.title" as const,
          },
        ]
      : []),
    { path: "export", icon: Download, label: "common.export" as const },
    {
      path: "settings",
      icon: Settings2,
      label: "common.projectSettings" as const,
    },
    { path: "events", icon: ScrollText, label: "common.eventLog" as const },
  ];
  return (
    <nav aria-label={t("navigation.project")} className="space-y-1">
      <p
        className={
          collapsed
            ? "sr-only"
            : "px-3 pb-1 text-xs text-muted-foreground truncate"
        }
        title={name}
      >
        {name || pid}
      </p>
      <div className="flex flex-wrap md:block">
        <NavigationLink
          to={base}
          icon={Sparkles}
          label="common.translationOverview"
          collapsed={collapsed}
          end
        />
        {book && (
          <>
            <NavigationLink
              to={`${base}/proofreading`}
              icon={BookOpenCheck}
              label="progress.manualProofreading"
              collapsed={collapsed}
            />
            <NavigationLink
              to={`${base}/review`}
              icon={ListChecks}
              label="common.wholeBookReview"
              collapsed={collapsed}
            />
          </>
        )}
        {format === "srt" && (
          <NavigationLink
            to={`${base}/subtitles`}
            icon={Captions}
            label="common.subtitleEditor"
            collapsed={collapsed}
          />
        )}
        {links.map(({ path, icon, label }) => (
          <NavigationLink
            key={path}
            to={`${base}/${path}`}
            icon={icon}
            label={label}
            collapsed={collapsed}
          />
        ))}
      </div>
    </nav>
  );
}
