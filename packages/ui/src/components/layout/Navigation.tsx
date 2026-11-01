import { NavLink, useLocation, useNavigate } from "react-router-dom";
import { ArrowLeft, type LucideIcon } from "lucide-react";
import { useI18n } from "../../i18n";
import { cn } from "../../lib/utils";
import { Button } from "../ui/button";
import {
  availableSettingsSections, isVisibleFor, navEntries, navPath, settingsPath,
} from "../../routes/manifest";

export function SettingsNavigation({ collapsed }: { collapsed: boolean }) {
  const { t, locale } = useI18n();
  const location = useLocation();
  const navigate = useNavigate();
  return (
    <>
      <nav
        aria-label={t("navigation.settings")}
        className={cn("min-h-0 flex-1 space-y-1 overflow-y-auto p-3", collapsed && "px-2")}
      >
        {availableSettingsSections(locale, t).map((entry) => (
          <NavigationLink
            key={entry.id}
            to={settingsPath(entry.id)}
            icon={entry.icon}
            label={entry.label}
            collapsed={collapsed}
            end
            state={location.state}
          />
        ))}
      </nav>
      <div
        data-slot="sidebar.footer"
        className={cn("shrink-0 p-3", collapsed && "p-2")}
      >
        <Button
          type="button"
          variant="ghost"
          className={cn("w-full justify-start gap-2", collapsed && "justify-center px-2")}
          aria-label={t("navigation.back")}
          title={collapsed ? t("navigation.back") : undefined}
          onClick={() => navigate(location.state?.settingsReturnTo ?? "/")}
        >
          <ArrowLeft className="h-4 w-4 shrink-0" aria-hidden="true" />
          {!collapsed && t("navigation.back")}
        </Button>
      </div>
    </>
  );
}

export function NavigationLink({
  to,
  icon: Icon,
  label,
  end,
  collapsed = false,
  prefetch,
  className,
  id,
  state,
}: {
  to: string;
  icon: LucideIcon;
  label: string;
  end?: boolean;
  collapsed?: boolean;
  /** Chunk loader warmed on hover/focus so the first click renders instantly. */
  prefetch?: () => Promise<unknown>;
  /** Extra classes merged last (e.g. the sidebar's primary-action slot). */
  className?: string;
  id?: string;
  state?: Record<string, unknown>;
}) {
  return (
    <NavLink
      id={id}
      to={to}
      state={state}
      end={end}
      title={collapsed ? label : undefined}
      onPointerEnter={() => void prefetch?.().catch(() => {})}
      onFocus={() => void prefetch?.().catch(() => {})}
      className={({ isActive }) =>
        cn(
          "flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm transition-[color,background-color,padding,gap] duration-sidebar ease-sidebar",
          collapsed && "gap-0 px-2",
          isActive
            ? "bg-accent text-accent-foreground font-medium"
            : "text-muted-foreground hover:text-foreground hover:bg-accent/50",
          className,
        )
      }
    >
      <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
      <span
        className={cn(
          "min-w-0 max-w-48 flex-1 overflow-hidden whitespace-nowrap transition-[max-width,opacity,transform] duration-sidebar ease-sidebar",
          collapsed && "max-w-0 -translate-x-2 opacity-0",
        )}
      >
        {label}
      </span>
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
  const links = navEntries
    .filter(
      (entry) =>
        entry.group === "project" && isVisibleFor(entry.audience, format),
    )
    .sort((a, b) => a.order - b.order);
  return (
    <nav aria-label={t("navigation.project")} className="space-y-1">
      <p
        className={cn(
          "max-h-5 overflow-hidden truncate px-3 pb-1 text-xs text-muted-foreground transition-[max-height,padding,opacity] duration-sidebar ease-sidebar",
          collapsed && "max-h-0 pb-0 opacity-0",
        )}
        title={name}
      >
        {name || pid}
      </p>
      <div>
        {links.map((entry) => (
          <NavigationLink
            key={entry.to}
            to={navPath(entry, pid)}
            icon={entry.icon}
            label={t(entry.label)}
            collapsed={collapsed}
            end={entry.end}
            prefetch={entry.loader}
          />
        ))}
      </div>
    </nav>
  );
}
