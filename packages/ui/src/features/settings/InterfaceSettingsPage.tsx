import { Braces, Languages, Plug, SlidersHorizontal } from "lucide-react";
import { Navigate, useLocation, useParams } from "react-router-dom";
import { Brand, PageContainer, PageHeader } from "@/components/layout/AppLayout";
import { NavigationLink, ProjectListNavigation } from "@/components/layout/Navigation";
import { SelectDismissScope } from "@/components/ui/select";
import { useI18n } from "@/i18n";
import { GlobalConfiguration } from "./GlobalConfiguration";
import { LanguageSettings } from "./LanguageSettings";

const sections = [
  {
    id: "interface",
    to: "/settings",
    icon: Languages,
    label: "settings.interfaceLanguage",
  },
  {
    id: "providers",
    to: "/settings/providers",
    icon: Plug,
    label: "providerSettings.apiProvidersModels",
  },
  {
    id: "defaults",
    to: "/settings/defaults",
    icon: SlidersHorizontal,
    label: "settings.newProjectDefaults",
  },
  {
    id: "advanced",
    to: "/settings/advanced",
    icon: Braces,
    label: "settings.advancedYamlConfiguration",
  },
] as const;

export default function InterfaceSettingsPage() {
  const { t } = useI18n();
  const location = useLocation();
  const { section: routeSection } = useParams();
  const active = sections.find(({ id }) => id === (routeSection ?? "interface"));
  if (!active || routeSection === "interface")
    return <Navigate to="/settings" replace />;
  const section = active.id;

  return (
    <div className="flex h-dvh flex-col overflow-hidden md:flex-row">
      <aside className="flex min-h-0 shrink-0 flex-col border-b bg-card md:w-60 md:border-b-0 md:border-r">
        <div className="flex h-16 shrink-0 items-center border-b px-4">
          <Brand />
        </div>
        <nav
          aria-label={t("navigation.settings")}
          className="grid min-h-0 grid-cols-2 gap-1 p-3 md:flex-1 md:content-start md:grid-cols-1 md:overflow-y-auto [&_span]:min-w-0 [&_span]:break-words"
        >
          {sections.map(({ id, to, icon, label }) => (
            <NavigationLink key={id} to={to} icon={icon} label={label} end />
          ))}
        </nav>
        <ProjectListNavigation />
      </aside>
      <main className="min-h-0 min-w-0 flex-1 overflow-y-auto">
        <PageHeader
          title={t("settings.title")}
          subtitle={t("settings.subtitle")}
        />
        <PageContainer className="max-w-5xl space-y-4 px-4 sm:px-6">
          <SelectDismissScope dismissKey={location.key}>
            <div hidden={section !== "interface"}>
              <LanguageSettings />
            </div>
            <div hidden={section === "interface"} className="space-y-4">
              <GlobalConfiguration section={section} />
            </div>
          </SelectDismissScope>
        </PageContainer>
      </main>
    </div>
  );
}
