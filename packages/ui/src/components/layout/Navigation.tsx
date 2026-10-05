import { NavLink } from "react-router-dom";
import { type LucideIcon } from "lucide-react";
import { useI18n, type MessageKey } from "@/i18n";
import { cn } from "@/lib/utils";
import { isVisibleFor, navEntries, navPath } from "@/routes/manifest";

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
  const links = navEntries
    .filter(
      (entry) =>
        entry.group === "project" && isVisibleFor(entry.audience, format),
    )
    .sort((a, b) => a.order - b.order);
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
        {links.map((entry) => (
          <NavigationLink
            key={entry.to}
            to={navPath(entry, pid)}
            icon={entry.icon}
            label={entry.label}
            collapsed={collapsed}
            end={entry.end}
          />
        ))}
      </div>
    </nav>
  );
}
